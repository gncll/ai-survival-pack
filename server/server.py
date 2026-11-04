#!/usr/bin/env python3
"""AI Survival Pack server. Standard library only, so it runs with no internet and no pip.

Serves the homepage and maps, proxies the Kiwix library, and answers questions with a local
Gemma model that may only use passages it finds in the library. Kiwix and Ollama stay on
localhost. Only this server is reachable from other devices on the network.
"""
import http.client
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WWW = ROOT / "www"
MAPS = ROOT / "maps"
LIBRARY = Path(os.environ.get("LIBRARY", ROOT / "library"))
PDF_INDEX = LIBRARY / "pdf-index.sqlite"
PORT = int(os.environ.get("PORT", 8080))
KIWIX_HOST, KIWIX_PORT = "127.0.0.1", int(os.environ.get("KIWIX_PORT", 8081))
OLLAMA_HOST, OLLAMA_PORT = "127.0.0.1", 11434
MODEL = os.environ.get("SURVIVAL_MODEL", "gemma4:e2b-it-qat")

MAX_SOURCES = 4
MAX_PER_DOC = 3
RERANK_POOL = 10
GUIDE_BOOKS = ("ready.gov", "nhs.uk")  # Short official pages. Read neighbouring sections too.
MAX_GUIDE_CHARS = 3200
CHUNK_CHARS = 1400
MIN_SCORE = 0.35

# Practical sources outrank the general encyclopedia when scores are close.
BOOK_BOOST = {
    "nhs.uk": 1.35, "ready.gov": 1.35, "irp.fas.org": 1.0, "zimgit-water": 1.2, "zimgit-food": 1.15,
    "zimgit-knots": 1.2, "zimgit-medicine": 0.95, "zimgit-post-disaster": 0.9, "wikipedia_en_medicine": 1.25, "mdwiki": 1.2,
    "outdoors.stackexchange": 0.75, "wikipedia_en_top": 0.9, "wikipedia_tr_top": 0.65, "solar.lowtech": 0.8,
}
STOPWORDS = set(
    "a an and are as at be but by can do does for from get has have how i if in into is it its me my "
    "of on or our should so some than that the their them then there these they this to up us was we "
    "what when where which who why will with you your need want make use used using after before during "
    "while about would could there here tell give find way ways best good safe safely step steps guide "
    "bir ve ile için ne nasıl neden bu şu o da de mi mı mu mü en çok gibi kadar sonra önce var yok "
    "yapmalıyım yapmam gerekir nedir olur olursa".split()
)
GENERIC_TOPIC = set("safety treatment treat treating response preparedness emergency emergencies first aid care "
                    "dosage dose method methods procedure procedures management control guide tips help "
                    "heavy severe long dirty big small large adult child person someone people".split())
LANG_NAMES = {"tr": "Turkish", "en": "English", "de": "German", "fr": "French", "es": "Spanish",
              "ar": "Arabic", "ru": "Russian", "pt": "Portuguese", "it": "Italian", "nl": "Dutch"}

PLAN_PROMPT = """You turn a person's question into keyword searches for an offline library of medical, survival and repair books.
Reply with JSON only: {"language":"<ISO 639-1 code of the question>","question_en":"<the question in English>","topic_en":["<word>"],"topic_native":["<word>"],"english":["<keywords>","<keywords>"],"native":["<keywords>"]}
topic_en: 1 or 2 single English words naming the specific subject, the rarest and most specific nouns in the question. Never a generic word like safety, treatment, emergency, aid, dose, method or kit by itself. A brand or drug name goes here exactly as written.
topic_native: the same subject in the language of the question. Use [] when the question is in English.
english: 2 searches of 2 to 5 English keywords. Use plain topic words and the proper medical or technical term. No question words.
native: 1 search in the language of the question. Use [] when the question is in English.

Examples:
Question: What do I do for a burn on my arm?
{"language":"en","question_en":"What do I do for a burn on my arm?","topic_en":["burn"],"topic_native":[],"english":["burn first aid cool water","burn wound treatment"],"native":[]}
Question: Sel gelirse ne yapmaliyim?
{"language":"tr","question_en":"What should I do if a flood comes?","topic_en":["flood"],"topic_native":["sel"],"english":["flood safety evacuation","flood what to do"],"native":["sel güvenlik"]}
Question: Can I eat food from a dented can?
{"language":"en","question_en":"Can I eat food from a dented can?","topic_en":["dented","canned"],"topic_native":[],"english":["dented canned food safety","food spoilage botulism"],"native":[]}
Question: Is the pill called Xyzol safe in pregnancy?
{"language":"en","question_en":"Is the pill called Xyzol safe in pregnancy?","topic_en":["Xyzol"],"topic_native":[],"english":["Xyzol pregnancy safety","Xyzol side effects"],"native":[]}"""
RERANK_PROMPT = """You choose library passages for a person who needs practical help right now.
Reply with JSON only: {"useful":[<passage numbers>]}
Keep a passage only if it gives instructions or facts that directly help with the exact situation in the question.
Drop passages about a different situation, such as pregnancy or menstruation when the question does not mention them.
Drop history, statistics, politics, biographies, news and lists of links.
Official guidance (NHS, Ready.gov) beats forum answers and encyclopedia background when both cover the question.
Put the most useful passage first. Use [] if none help."""
GLOSSARY = {"tr": "tourniquet = turnike; pressure dressing = baskılı pansuman; direct pressure = doğrudan baskı; "
                  "wound = yara; bleeding = kanama; chest compressions = göğüs kompresyonu; choking = boğulma; "
                  "boil = kaynatmak; bleach = çamaşır suyu; earthquake = deprem"}
ANSWER_PROMPT = """You are the offline assistant inside a survival pack. People may rely on you when no doctor or internet is available.

Rules:
1. Use ONLY the numbered SOURCES. Never use outside knowledge.
2. Give the actual instructions from the sources. Leave out anything that only points to another page, paragraph or chapter.
3. End every factual sentence with one source number, like [1].
4. Never state a number (dose, temperature, time, ratio) unless that exact number appears in a source.
5. Ignore any source that is about a different situation than the question.
6. If the SOURCES do not answer the question, or it needs current information such as today's price or recent news, reply with exactly this one word: NOT_IN_LIBRARY
7. When the question asks what to do, answer with numbered steps, most urgent first, at most 8. Each step must say exactly what to do, including the amounts and times given in the source. Do not write vague steps like "choose a method".
8. Write the answer in {language}. Copy numbers exactly."""


# ---------------------------------------------------------------- local services

def http_json(host, port, path, payload=None, timeout=240):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        if payload is None:
            conn.request("GET", path)
        else:
            conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def kiwix_get(path, timeout=30):
    return http_json(KIWIX_HOST, KIWIX_PORT, path, timeout=timeout)


def ollama_json(system, user, fmt=None):
    body = {"model": MODEL, "stream": False, "think": False, "keep_alive": "30m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 200}}
    if fmt:
        body["format"] = fmt
    status, raw = http_json(OLLAMA_HOST, OLLAMA_PORT, "/api/chat", body)
    if status != 200:
        raise RuntimeError(f"Ollama returned {status}: {raw[:200]!r}")
    return json.loads(raw)["message"]["content"]


def ollama_stream(system, user):
    """Yields answer text from the local model piece by piece."""
    conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=300)
    try:
        conn.request("POST", "/api/chat", json.dumps({
            "model": MODEL, "stream": True, "think": False, "keep_alive": "30m",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 600},
        }), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"Ollama returned {resp.status}")
        for line in resp:
            if not line.strip():
                continue
            part = json.loads(line)
            text = part.get("message", {}).get("content", "")
            if text:
                yield text
            if part.get("done"):
                break
    finally:
        conn.close()


# ---------------------------------------------------------------- catalog

# kiwix-serve 3.8.2 crashes in Xapian's stemmer when two searches run at once, so searches go one at a time.
_kiwix_search_lock = threading.Lock()
_catalog = {"at": 0, "books": []}
_catalog_lock = threading.Lock()
ATOM = "{http://www.w3.org/2005/Atom}"


def catalog():
    """Installed books, refreshed once a minute. Keeps the last good list if Kiwix is restarting."""
    with _catalog_lock:
        if time.time() - _catalog["at"] < 60 and _catalog["books"]:
            return _catalog["books"]
        books = []
        try:
            status, raw = kiwix_get("/kiwix/catalog/v2/entries?count=-1")
            if status == 200:
                for e in ET.fromstring(raw).findall(f"{ATOM}entry"):
                    href = next((l.get("href") for l in e.findall(f"{ATOM}link")
                                 if l.get("type") == "text/html"), None)
                    if not href:
                        continue
                    books.append({
                        "id": href.rstrip("/").rsplit("/", 1)[-1],
                        "title": e.findtext(f"{ATOM}title", ""),
                        "lang": e.findtext(f"{ATOM}language", ""),
                        "searchable": "_ftindex:yes" in e.findtext(f"{ATOM}tags", ""),
                    })
        except (OSError, ET.ParseError):
            return _catalog["books"]
        if books:
            _catalog.update(at=time.time(), books=books)
        return _catalog["books"]


def boost(book_id):
    return next((v for k, v in BOOK_BOOST.items() if book_id.startswith(k)), 1.0)


# ---------------------------------------------------------------- reading articles

class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "sup", "svg", "button"}
    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "div", "section", "br", "dd", "dt", "blockquote"}
    HEAD = {"h1", "h2", "h3", "h4"}
    STOP_SECTIONS = re.compile(r"^(references|external links|see also|further reading|notes|sources|bibliography|"
                               r"kaynakça|dış bağlantılar|ayrıca bakınız)\b", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip_depth, self.in_head, self.stopped = [], 0, False, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag in self.HEAD:
            self.in_head = True
            self.parts.append("\n## ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag in self.HEAD:
            self.in_head = False
            self.parts.append("\n")
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth or self.stopped:
            return
        if self.in_head and self.STOP_SECTIONS.match(data.strip()):
            self.stopped = True
            return
        self.parts.append(data)


def html_to_text(raw):
    parser = TextExtractor()
    parser.feed(raw.decode("utf-8", "replace"))
    text = "".join(parser.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def chunks_of(text):
    """Splits text at paragraph breaks and keeps the section heading with each chunk."""
    out, cur, heading = [], "", ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("## "):
            if len(cur) > 200:
                out.append(cur.strip())
                cur = ""
            heading = para[3:].strip()
            continue
        if len(cur) + len(para) > CHUNK_CHARS and cur:
            out.append(cur.strip())
            cur = ""
        if not cur and heading:
            cur = f"[{heading}] "
        cur += para + " "
    if cur.strip():
        out.append(cur.strip())
    return [c[:CHUNK_CHARS + 300] for c in out]


def terms_of(*queries):
    terms = []
    for q in queries:
        for word in re.findall(r"[^\W\d_]{3,}", q.lower()):
            if word not in STOPWORDS and word not in terms:
                terms.append(word)
    return terms


def stem_pattern(term):
    # A prefix match handles plurals and short suffixes without a stemmer.
    return re.compile(r"\b" + re.escape(term[:max(4, len(term) - 2)]), re.I)


def rarity_weights(patterns, chunks):
    """Rare terms carry the topic. Common ones like 'emergency' or 'treatment' should not decide the match."""
    total = max(1, len(chunks))
    weights = []
    for p in patterns:
        df = sum(1 for c in chunks if p.search(c))
        weights.append(math.log(1 + total / (1 + df)))
    return weights


def score_chunk(chunk, title, patterns, weights):
    if not patterns:
        return 0.0
    total = sum(weights) or 1.0
    hits = [len(p.findall(chunk)) for p in patterns]
    coverage = sum(w for h, w in zip(hits, weights) if h) / total
    if not coverage:
        return 0.0
    density = sum(min(h, 4) * w for h, w in zip(hits, weights)) / (4 * total)
    title_hit = sum(w for p, w in zip(patterns, weights) if p.search(title)) / total
    return coverage * coverage + 0.5 * density + 0.3 * title_hit


def dedupe_key(text):
    # The same PDF ships in more than one collection. Compare passages by their letters only.
    return re.sub(r"[^a-z]", "", text.lower())[:160]


# ---------------------------------------------------------------- searching

ITEM_LIMIT = 6
MAX_DOCS = 60
NOISE_TITLE = re.compile(r"^(highest voted|newest|recently active|frequent|hot) |^(category|talk|template|portal):|^questions/", re.I)
# Ready.gov and similar captures hold a translated copy of every page. The English page is the one to read.
TRANSLATED_COPY = re.compile(r"/(ar|de|es|fr|hi|ht|it|ja|ko|nl|pl|pt|pt-br|ru|tl|tr|ur|vi|zh-hans|zh-hant)/(node/|\w)")
BOILERPLATE = re.compile(r"official websites use \.gov|skip to main content|cookies? (policy|settings)|kreyòl", re.I)
ACTION_HEADING = re.compile(r"^\[[^\]]*\b(treatment|management|first aid|what to do|how to|during|stay safe|"
                            r"if you|symptoms and|emergency action|dose|dosage|how much|how to take|supplies)\b", re.I)


def search_book(book_id, query):
    q = urllib.parse.urlencode({"pattern": query, "format": "xml", "pageLength": ITEM_LIMIT, "books.name": book_id})
    try:
        with _kiwix_search_lock:
            status, raw = kiwix_get(f"/kiwix/search?{q}")
        if status != 200:
            return []
        return [{"title": it.findtext("title", "").strip(), "link": it.findtext("link", ""), "book": book_id}
                for it in ET.fromstring(raw).iter("item")]
    except (OSError, ET.ParseError):
        return []


def fetch_doc(item):
    """Reads one article through Kiwix and returns its text chunks."""
    prefix = f"/kiwix/content/{item['book']}/"
    if not item["link"].startswith(prefix):
        return item, []
    path = item["link"][len(prefix):]
    try:
        status, raw = kiwix_get(f"/kiwix/raw/{item['book']}/content/{path}")
    except OSError:
        return item, []
    if status != 200:
        return item, []
    return item, chunks_of(html_to_text(raw))


def fts_query(terms):
    return " OR ".join(f'"{t}"*' for t in terms)


def search_pdfs(terms, limit=14):
    """Full-text search over the PDF collections Kiwix cannot search."""
    if not PDF_INDEX.exists() or not terms:
        return []
    db = sqlite3.connect(f"file:{PDF_INDEX}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT book, path, title, page, text FROM pages WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?",
            (fts_query(terms), limit)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    return [{"book": b, "path": p, "title": t, "page": pg, "text": x} for b, p, t, pg, x in rows]


def plan_query(question):
    words = " ".join(terms_of(question)) or question
    try:
        plan = json.loads(ollama_json(PLAN_PROMPT, question, fmt="json"))
    except (ValueError, RuntimeError, OSError):
        plan = {}
    english = [q for q in plan.get("english", []) if isinstance(q, str) and q.strip()][:2] or [words]
    language = str(plan.get("language", "en"))[:2].lower()
    topic_en = terms_of(*[t for t in plan.get("topic_en", []) if isinstance(t, str)])
    topic_en = [t for t in topic_en if t not in GENERIC_TOPIC][:3]
    if not topic_en:  # Fall back to the most specific word of the first search.
        topic_en = [t for t in terms_of(english[0]) if t not in GENERIC_TOPIC][:1]
    topic_native = terms_of(*[t for t in plan.get("topic_native", []) if isinstance(t, str)])[:3]
    if language == "en":
        topic_native = []
    native = [q for q in plan.get("native", []) if isinstance(q, str) and q.strip()][:1]
    if language == "en":
        native = []  # The model sometimes invents a "native" query for English questions.
    question_en = plan.get("question_en")
    question_en = question_en.strip() if isinstance(question_en, str) and question_en.strip() else question
    return {"language": language, "question_en": question_en, "topic_en": topic_en, "topic_native": topic_native,
            "english": english, "native": native}


def retrieve(question, plan):
    """Returns the best library passages for a question, highest score first."""
    books = [b for b in catalog() if b["searchable"]]
    jobs = []
    for b in books:
        if b["lang"] == "tur":
            if plan["language"] != "tr":
                continue
            queries, topic = plan["native"], plan["topic_native"]
        else:
            queries, topic = plan["english"], plan["topic_en"]
        jobs += [(b["id"], q) for q in queries]
        if topic:
            jobs.append((b["id"], " ".join(topic)))  # Surfaces pages about the topic itself.

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = [search_book(*j) for j in jobs]
        seen, items = set(), []
        for res in found:
            for it in res:
                translated = "ready.gov" in it["book"] and TRANSLATED_COPY.search(it["link"].split("/www.ready.gov", 1)[-1])
                if it["link"] not in seen and not NOISE_TITLE.search(it["title"]) and not translated:
                    seen.add(it["link"])
                    items.append(it)
        docs = list(pool.map(fetch_doc, items[:MAX_DOCS]))

    terms_en = terms_of(*plan["english"], *plan["topic_en"])
    terms_all = terms_of(*plan["native"], *plan["topic_native"], question)
    english_patterns = [stem_pattern(t) for t in terms_en]
    all_patterns = [stem_pattern(t) for t in terms_all]

    candidates = []
    doc_chunks = {}
    for item, chunks in docs:
        doc_chunks[item["link"]] = chunks
        for i, chunk in enumerate(chunks):
            candidates.append({"text": chunk, "title": item["title"], "book": item["book"],
                               "url": item["link"], "doc": item["link"], "index": i})
    for row in search_pdfs(terms_en):
        page_path = urllib.parse.quote(row["path"])
        for chunk in chunks_of(row["text"]):
            candidates.append({"text": chunk, "title": f"{row['title']}, page {row['page']}", "book": row["book"],
                               "url": f"/kiwix/content/{row['book']}/{page_path}#page={row['page']}",
                               "doc": f"{row['book']}/{row['path']}#{row['page']}"})

    # A passage that never mentions the topic cannot answer the question, however many other words match.
    topic_en = [stem_pattern(t) for t in plan["topic_en"]]
    topic_native = [stem_pattern(t) for t in plan["topic_native"]]
    question_words = [stem_pattern(w) for w in re.findall(r"[^\W\d_]{4,}", f"{question} {plan['question_en']}".lower())]
    kept = []
    for c in candidates:
        if BOILERPLATE.search(c["text"]):
            continue
        topic = topic_native if "_tr_" in c["book"] else topic_en
        c["topic_title"] = any(p.search(c["title"]) for p in topic)
        if not topic or c["topic_title"] or any(p.search(c["text"]) for p in topic):
            kept.append(c)
    candidates = kept

    texts = [c["text"] for c in candidates]
    english_weights = rarity_weights(english_patterns, texts)
    all_weights = rarity_weights(all_patterns, texts)
    scored = []
    for c in candidates:
        turkish = "_tr_" in c["book"]
        patterns, weights = (all_patterns, all_weights) if turkish else (english_patterns, english_weights)
        s = score_chunk(c["text"], c["title"], patterns, weights) * boost(c["book"])
        if s <= 0:
            continue
        if c["topic_title"]:
            s += 0.35  # A page about the topic beats a page that mentions it in passing.
        heading = c["text"][:c["text"].find("]") + 1] if c["text"].startswith("[") else ""
        if ACTION_HEADING.search(c["text"]):
            s += 0.2  # Instructions beat background.
        if heading and any(p.search(heading) for p in question_words):
            s += 0.2  # "[During an Earthquake]" answers "what do I do during an earthquake".
        scored.append({**c, "score": s})
    scored.sort(key=lambda r: r["score"], reverse=True)

    ranked, per_doc = [], {}
    for r in scored:
        if r["score"] < MIN_SCORE:
            break
        if per_doc.get(r["doc"], 0) >= MAX_PER_DOC:
            continue
        per_doc[r["doc"]] = per_doc.get(r["doc"], 0) + 1
        ranked.append(r)
    if not ranked:
        return []
    english_hint = plan["question_en"] if plan["language"] != "en" else ""
    return expand_guides(rerank(question, ranked, english_hint), doc_chunks)


def expand_guides(sources, doc_chunks):
    """Merges picks from one official guide page and adds the sections next to them.

    A kit list or a dose table often sits one section away from the passage that matched.
    """
    merged, by_doc = [], {}
    for src in sources:
        if not src["book"].startswith(GUIDE_BOOKS) or "index" not in src:
            merged.append(src)
        elif src["doc"] in by_doc:
            by_doc[src["doc"]]["picks"].add(src["index"])
        else:
            entry = {**src, "picks": {src["index"]}}
            by_doc[src["doc"]] = entry
            merged.append(entry)
    for entry in by_doc.values():
        chunks = doc_chunks[entry["doc"]]
        wanted = sorted({n for p in entry["picks"] for n in (p, p - 1, p + 1) if 0 <= n < len(chunks)},
                        key=lambda n: (min(abs(n - p) for p in entry["picks"]), n))
        taken, size = [], 0
        for n in wanted:
            if BOILERPLATE.search(chunks[n]) or size + len(chunks[n]) > MAX_GUIDE_CHARS:
                continue
            taken.append(n)
            size += len(chunks[n])
        entry["text"] = "\n".join(chunks[n] for n in sorted(taken))
        del entry["picks"]
    return merged


def rerank(question, ranked, english_hint=""):
    """Asks the model which of the top passages fit the situation. Keeps the ranking if it gives no usable reply."""
    pool, seen_text = [], set()
    for r in ranked:
        key = dedupe_key(r["text"])
        if key not in seen_text:
            seen_text.add(key)
            pool.append(r)
        if len(pool) >= RERANK_POOL:
            break
    listing = "\n\n".join(f"Passage {i + 1}: {r['title']} ({book_title(r['book'])})\n{r['text'][:420]}"
                          for i, r in enumerate(pool))
    asked = f"{question} (in English: {english_hint})" if english_hint else question
    try:
        reply = json.loads(ollama_json(RERANK_PROMPT, f"QUESTION: {asked}\n\n{listing}", fmt="json"))
        picks = [int(n) for n in reply.get("useful", []) if 1 <= int(n) <= len(pool)]
    except (ValueError, TypeError, RuntimeError, OSError):
        return pool[:MAX_SOURCES]
    order = list(dict.fromkeys(picks))
    return [pool[n - 1] for n in order][:MAX_SOURCES] or pool[:MAX_SOURCES]


def book_title(book_id):
    return next((b["title"] for b in catalog() if b["id"] == book_id), book_id)


def unverified_numbers(answer, sources):
    """Numbers in the answer that appear in no source, excluding citations and step labels."""
    clean = re.sub(r"\[[\d,\s]+\]", "", answer)
    clean = re.sub(r"(?m)^\s*\d+[.)]\s", "", clean)
    source_text = " ".join(s["text"] for s in sources)
    source_nums = set(re.findall(r"\d+(?:[.,]\d+)?", source_text))
    return sorted({n for n in re.findall(r"\d+(?:[.,]\d+)?", clean)
                   if n not in source_nums and n.replace(",", ".") not in source_nums})


def answer_events(question):
    """Yields the events of one answer as dictionaries."""
    started = time.time()
    yield {"type": "status", "text": "Reading your question"}
    plan = plan_query(question)
    yield {"type": "status", "text": "Searching the library: " + " | ".join(plan["english"] + plan["native"])}
    sources = retrieve(question, plan)
    public = [{"n": i + 1, "title": s["title"], "book": book_title(s["book"]), "url": s["url"],
               "excerpt": s["text"]} for i, s in enumerate(sources)]
    yield {"type": "sources", "items": public, "plan": plan}
    if not sources:
        yield {"type": "not_found"}
        yield {"type": "done", "seconds": round(time.time() - started, 1)}
        return

    yield {"type": "status", "text": f"Gemma is reading {len(sources)} passages"}
    context = "\n\n".join(f"[{i + 1}] {s['title']} ({book_title(s['book'])})\n{s['text']}"
                          for i, s in enumerate(sources))
    system = ANSWER_PROMPT.format(language=LANG_NAMES.get(plan["language"], "the language of the question"))
    if plan["language"] in GLOSSARY:
        system += f"\n9. Translate these terms exactly: {GLOSSARY[plan['language']]}."
    user = (f"SOURCES:\n{context}\n\nQUESTION: {question}\n\n"
            "Remember: use only the sources, and end every sentence with its source number like [1].")

    answer = ""
    held = ""
    for piece in ollama_stream(system, user):
        answer += piece
        # Hold the first characters back so NOT_IN_LIBRARY never reaches the reader as an answer.
        if len(answer) < 16:
            held += piece
            continue
        yield {"type": "token", "text": held + piece}
        held = ""
    if "NOT_IN_LIBRARY" in answer:
        yield {"type": "not_found"}
    else:
        if held:
            yield {"type": "token", "text": held}
        unverified = unverified_numbers(answer, sources)
        cited = re.findall(r"\[(\d+(?:,\s*\d+)*)\]", answer)
        numbers = [int(n) for group in cited for n in group.split(",")]
        bad_citation = any(n < 1 or n > len(sources) for n in numbers)
        if unverified or not cited or bad_citation:
            yield {"type": "warning", "numbers": unverified, "uncited": not cited, "bad_citation": bad_citation}
    yield {"type": "done", "seconds": round(time.time() - started, 1)}


# ---------------------------------------------------------------- info

def lan_urls():
    urls = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # Picks the active interface. Sends no packets.
            urls.append(f"http://{s.getsockname()[0]}:{PORT}")
    except OSError:
        pass
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2).stdout.strip()
        if name:
            urls.append(f"http://{name}.local:{PORT}")
    except (OSError, subprocess.SubprocessError):
        pass
    return urls


def info():
    zims = list(LIBRARY.glob("*.zim"))
    maps = sorted(p.name for p in MAPS.glob("*.pmtiles")) if MAPS.exists() else []
    try:
        status, raw = http_json(OLLAMA_HOST, OLLAMA_PORT, "/api/tags", timeout=3)
        names = [m["name"] for m in json.loads(raw).get("models", [])] if status == 200 else []
    except (OSError, ValueError):
        names = []
    pdf_books = pdf_pages = 0
    if PDF_INDEX.exists():
        db = sqlite3.connect(f"file:{PDF_INDEX}?mode=ro", uri=True)
        try:
            pdf_books, pdf_pages = db.execute("SELECT sum(with_text), sum(pages) FROM done").fetchone()
        except sqlite3.Error:
            pass
        finally:
            db.close()
    return {
        "urls": lan_urls(),
        "model": MODEL,
        "model_ready": MODEL in names,
        "books": len(zims),
        "library_gb": round(sum(p.stat().st_size for p in zims) / 1e9, 1),
        "maps": maps,
        "maps_gb": round(sum((MAPS / m).stat().st_size for m in maps) / 1e9, 1),
        "pdf_files": pdf_books,
        "pdf_pages": pdf_pages,
        "free_gb": round(shutil.disk_usage(LIBRARY).free / 1e9, 1),
    }


# ---------------------------------------------------------------- HTTP

mimetypes.add_type("application/octet-stream", ".pmtiles")
mimetypes.add_type("application/x-protobuf", ".pbf")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SurvivalPack/1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_text(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/kiwix" or path.startswith("/kiwix/"):
            return self.proxy_kiwix()
        if path == "/api/info":
            return self.send_json(info())
        if path.startswith("/maps/data/"):
            return self.send_file(MAPS, urllib.parse.unquote(path[len("/maps/data/"):]))
        rel = urllib.parse.unquote(path.lstrip("/"))
        if not rel or rel.endswith("/"):
            rel += "index.html"
        return self.send_file(WWW, rel)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/api/ask":
            return self.send_error_text(404, "Not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self.send_error_text(413, "Question too long")
        try:
            question = str(json.loads(self.rfile.read(length)).get("question", "")).strip()[:500]
        except ValueError:
            return self.send_error_text(400, "Bad JSON")
        if not question:
            return self.send_error_text(400, "Empty question")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for event in answer_events(question):
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, RuntimeError) as e:
            try:
                self.wfile.write((json.dumps({"type": "error", "text": str(e)}) + "\n").encode())
            except OSError:
                pass

    def send_file(self, base, rel):
        base = base.resolve()
        target = (base / rel).resolve()
        if base not in target.parents or not target.is_file():
            return self.send_error_text(404, "Not found")
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        start, end, partial = 0, size - 1, False
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match and (match[1] or match[2]):
            if match[1]:
                start, end = int(match[1]), int(match[2]) if match[2] else size - 1
            else:
                start, end = max(0, size - int(match[2])), size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache" if ctype.startswith("text/html") else "max-age=3600")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    data = f.read(min(1 << 18, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    left -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def proxy_kiwix(self):
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() in ("range", "accept", "accept-encoding", "accept-language", "if-none-match")}
        conn = http.client.HTTPConnection(KIWIX_HOST, KIWIX_PORT, timeout=60)
        is_search = urllib.parse.urlsplit(self.path).path.rstrip("/") in ("/kiwix/search", "/kiwix/suggest")
        try:
            if is_search:
                with _kiwix_search_lock:
                    conn.request(self.command, self.path, headers=headers)
                    resp = conn.getresponse()
            else:
                conn.request(self.command, self.path, headers=headers)
                resp = conn.getresponse()
            self.send_response(resp.status)
            has_length = False
            for k, v in resp.getheaders():
                low = k.lower()
                if low in ("connection", "transfer-encoding", "keep-alive", "server", "date"):
                    continue
                has_length = has_length or low == "content-length"
                self.send_header(k, v)
            if not has_length:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                data = resp.read(1 << 16)
                if not data:
                    break
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except OSError as e:
            self.close_connection = True
            try:
                self.send_error_text(502, f"Kiwix is not running: {e}")
            except OSError:
                pass
        finally:
            conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


if __name__ == "__main__":
    print(f"AI Survival Pack on http://127.0.0.1:{PORT}", flush=True)
    for url in lan_urls():
        print(f"Other devices on this network: {url}", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
