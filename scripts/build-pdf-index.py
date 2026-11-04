#!/usr/bin/env python3
"""Builds a full-text index of the PDF collections that Kiwix cannot search.

The zimgit collections (water, medicine, food, knots, post-disaster) ship without a search index,
so the AI could never read them. This pulls each PDF out of the ZIM, reads the text page by page and
stores it in SQLite FTS5. Scanned PDFs without a text layer are counted and skipped.
Only this build step needs libzim and pymupdf. The server reads the result with the standard library.
"""
import re
import sqlite3
import sys
from pathlib import Path

import pymupdf
from libzim.reader import Archive

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "library"
DB_PATH = LIBRARY / "pdf-index.sqlite"
MIN_PAGE_CHARS = 200
pymupdf.TOOLS.mupdf_display_errors(False)


def clean(text):
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def good_title(text):
    # Rejects URLs, file names and sentence fragments, which PDF metadata and first lines are full of.
    return (8 < len(text) < 80 and text[0].isupper() and not text.endswith((".", ",", ";"))
            and not re.search(r"https?:|www\.|\.(pdf|doc|indd|qxd)$|untitled|microsoft", text, re.I)
            and bool(re.search(r"[A-Za-z]{4}", text)))


def pdf_title(doc, entry_title):
    title = (doc.metadata or {}).get("title", "").strip()
    if good_title(title):
        return title
    if doc.page_count:
        for line in doc[0].get_text().splitlines()[:12]:
            if good_title(line.strip()):
                return line.strip()
    return Path(entry_title).stem


def main():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(book UNINDEXED, path UNINDEXED, title, "
               "page UNINDEXED, text, tokenize='porter unicode61')")
    db.execute("CREATE TABLE IF NOT EXISTS done(book TEXT PRIMARY KEY, pdfs INT, with_text INT, pages INT)")
    finished = {r[0] for r in db.execute("SELECT book FROM done")}

    for zim in sorted(LIBRARY.glob("*.zim")):
        book = zim.stem
        if book in finished:
            continue
        # Book collections built by zimgit keep their text inside PDFs that Kiwix does not index.
        if not book.startswith("zimgit-"):
            continue
        archive = Archive(zim)
        pdfs = with_text = pages = 0
        for i in range(archive.all_entry_count):
            entry = archive.get_entry_by_id(i)
            if entry.is_redirect:
                continue
            item = entry.get_item()
            if "pdf" not in item.mimetype:
                continue
            pdfs += 1
            try:
                doc = pymupdf.open(stream=bytes(item.content), filetype="pdf")
            except (pymupdf.FileDataError, RuntimeError, ValueError):
                continue
            title = pdf_title(doc, entry.title)
            kept = 0
            for number in range(doc.page_count):
                try:
                    text = clean(doc[number].get_text())
                except (RuntimeError, ValueError):
                    continue
                if len(text) >= MIN_PAGE_CHARS:
                    db.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (book, entry.path, title, number + 1, text))
                    kept += 1
            pages += kept
            with_text += 1 if kept else 0
            doc.close()
        db.execute("INSERT OR REPLACE INTO done VALUES (?,?,?,?)", (book, pdfs, with_text, pages))
        db.commit()
        print(f"{book}: {pdfs} PDFs, {with_text} with text, {pages} pages", flush=True)

    db.execute("INSERT INTO pages(pages) VALUES('optimize')")
    db.commit()
    total = db.execute("SELECT sum(pdfs), sum(with_text), sum(pages) FROM done").fetchone()
    print(f"TOTAL {total[0]} PDFs, {total[1]} with text, {total[2]} pages, "
          f"{DB_PATH.stat().st_size / 1e6:.0f} MB index")


if __name__ == "__main__":
    main()
