#!/usr/bin/env python3
"""Scores the pack on real emergency questions and compares it with Gemma alone.

Usage: python3 tests/eval.py [--raw]
A question passes when the answer has all expected facts, cites a source, and uses no number
missing from the sources. Refusal questions pass only when the pack declines to answer.
Writes tests/results.md and tests/results.json.
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API = "http://127.0.0.1:8080/api/ask"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "gemma4:e2b-it-qat"

QUESTIONS = [
    ("How do I make dirty water safe to drink?", [r"boil", r"minute"]),
    ("How do I stop heavy bleeding?", [r"pressure"]),
    ("What should I do for a burn?", [r"cool|cold|running water"]),
    ("How do I give CPR to an adult?", [r"compress"]),
    ("Someone is choking. What do I do?", [r"abdominal thrust|heimlich|back blow|slap"]),
    ("How do I treat hypothermia?", [r"warm"]),
    ("What are the first aid steps for heat stroke?", [r"cool"]),
    ("What do I do after a snake bite?", [r"immobili|still|antivenom|hospital|medical"]),
    ("What should a 72-hour emergency kit contain?", [r"water", r"food", r"flashlight|radio"]),
    ("What should I do during an earthquake?", [r"drop|cover|hold on|table|desk"]),
    ("What should I do if a flood is coming?", [r"higher ground|high ground|evacuat"]),
    ("What should I do in a long power outage?", [r"generator|flashlight|refrigerator|carbon monoxide"]),
    ("What is metformin used for?", [r"type 2 diabetes"]),
    ("How much paracetamol can an adult take in 24 hours?", [r"\b(4|four|8|eight)\b"]),
    ("How do I tie a bowline knot?", [r"loop|rabbit|hole|standing"]),
    ("How can I store food without a fridge?", [r"dry|can|salt|smok|ferment"]),
    ("Kirli suyu nasıl içilebilir hale getiririm?", [r"kayna|klor|filtre"]),
    ("Ağır kanamayı nasıl durdururum?", [r"bas[ıi]n?[çc]?|bask|turnike|sarg"]),
    ("Deprem sırasında ne yapmalıyım?", [r"masa|siper|kapan|çök|korun|yere"]),
    ("What is the dose of zorvaxin for a child?", None),
    ("How much does Bitcoin cost today?", None),
    ("Who won the 2038 World Cup?", None),
]


def post(url, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=300)


def ask_pack(question):
    out = {"answer": "", "sources": [], "plan": {}, "refused": False, "warning": None, "seconds": None}
    with post(API, {"question": question}) as r:
        for line in r:
            e = json.loads(line)
            if e["type"] == "token":
                out["answer"] += e["text"]
            elif e["type"] == "sources":
                out["sources"] = [{"title": s["title"], "book": s["book"]} for s in e["items"]]
                out["plan"] = e["plan"]
            elif e["type"] == "not_found":
                out["refused"], out["answer"] = True, ""
            elif e["type"] == "warning":
                out["warning"] = e
            elif e["type"] == "done":
                out["seconds"] = e["seconds"]
    return out


def ask_raw(question):
    body = {"model": MODEL, "stream": False, "think": False, "messages": [{"role": "user", "content": question}],
            "options": {"temperature": 0.1, "num_predict": 300}}
    with post(OLLAMA, body) as r:
        return json.loads(r.read())["message"]["content"]


# Wrong words a small model has actually produced for life-critical terms.
FORBIDDEN = re.compile(r"tornavida", re.I)


def grade(result, expected):
    if expected is None:
        return result["refused"], "refused" if result["refused"] else "ANSWERED INSTEAD OF REFUSING"
    if result["refused"]:
        return False, "refused, the library should cover this"
    missing = [p for p in expected if not re.search(p, result["answer"], re.I)]
    w = result["warning"] or {}
    problems = [f"missing /{p}/" for p in missing]
    if FORBIDDEN.search(result["answer"]):
        problems.append("dangerous mistranslation")
    if w.get("numbers"):
        problems.append(f"numbers not in sources: {w['numbers']}")
    if w.get("uncited"):
        problems.append("no citations")
    if w.get("bad_citation"):
        problems.append("citation to a missing source")
    return not problems, "; ".join(problems) or "ok"


def main():
    raw_mode = "--raw" in sys.argv
    rows, passed = [], 0
    started = time.time()
    for q, expected in QUESTIONS:
        result = ask_pack(q)
        ok, why = grade(result, expected)
        passed += ok
        row = {"question": q, "ok": ok, "why": why, **result}
        if raw_mode:
            row["raw"] = ask_raw(q)
        rows.append(row)
        print(f"{'PASS' if ok else 'FAIL'} {result['seconds']:>5}s  {q}  [{why}]", flush=True)
    print(f"\n{passed}/{len(QUESTIONS)} passed in {time.time() - started:.0f}s")

    (HERE / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    md = [f"# Pack eval: {passed}/{len(QUESTIONS)} passed\n"]
    for r in rows:
        md.append(f"## {'PASS' if r['ok'] else 'FAIL'}: {r['question']}\n\n{r['why']} ({r['seconds']}s)\n")
        md.append("**Pack:** " + ("(refused)" if r["refused"] else r["answer"]) + "\n")
        md.extend(f"- source: {s['title']} / {s['book']}" for s in r["sources"])
        md.append("")
        if "raw" in r:
            md.append("**Gemma alone:** " + r["raw"] + "\n")
    (HERE / "results.md").write_text("\n".join(md))


if __name__ == "__main__":
    main()
