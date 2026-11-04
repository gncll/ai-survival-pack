# AI Survival Pack

An offline library and a local AI on your own computer. Wikipedia, medical references, disaster guides and maps,
plus Gemma answering questions from those sources. Any phone on the same Wi-Fi or hotspot can use it in a browser.

Built for macOS on Apple Silicon. Tested on an M3 Pro with 18 GB of RAM.

## Setup

Needs [Ollama](https://ollama.com), Python 3 and about 18 GB of free disk.

```bash
git clone https://github.com/gncll/ai-survival-pack.git
cd ai-survival-pack
./setup.sh
./start.sh
```

Or open Claude Code in an empty folder and send this:

```
Install https://github.com/gncll/ai-survival-pack for me. Read the README first, then run setup.sh and start.sh. Tell me when it opens at localhost:8080.
```

Open http://localhost:8080. The home page shows a QR code for phones.

Choose content in `config/content.conf`. The downloader stops before free disk falls under 12 GB (`MIN_FREE_GB`).
Put the big set on an SSD with `LIBRARY=/Volumes/YourSSD/library`.
Change the street-level map region with `REGION_NAME` and `REGION_BBOX`.

## How an answer works

1. Gemma turns the question into English search keywords and a topic word.
2. Kiwix searches every book. SQLite FTS5 searches the PDF collections Kiwix cannot search.
3. Passages that never mention the topic are dropped. If nothing is left, the pack says it does not know.
4. Gemma picks the passages that fit the situation, then answers only from them, citing each one.
5. The server flags any number in the answer that appears in no source.

## Phone with no computer

Install the Kiwix app and a ZIM file for reading, and Google AI Edge Gallery with Gemma for chat. They are two
separate apps, so the phone-only version has no source-grounded answers. This route was not tested for this project.

## Limits

- AI answers can be wrong. Always read the source passage. Old manuals can hold outdated advice.
- Tested only on an Apple Silicon Mac. Windows and Linux need other binaries in `scripts/install-tools.sh`.
- `tests/eval.py` checks answers against expected keywords. It is a smoke test, not a medical review.
- Books are almost all in English. Answers in other languages are machine translated by a small model.
- No built-in Wi-Fi hotspot or captive portal. Use a phone hotspot or your router.
- The server has no login. Anyone on your network can read the library and ask questions. Ollama and Kiwix stay on localhost.
