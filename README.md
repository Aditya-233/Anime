# AnimePahe

Minimal interactive AnimePahe downloader.

## Setup

From this directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python animepahe.py
```

The program asks for an anime, lets you navigate search results, then lets you
multi-select episodes with `Space`. Press `Enter` to download selected episodes.

Downloads are stored in:

```text
~/Downloads/<Anime Title>/
```

Controls:

```text
Up/Down  Navigate
Space    Select or deselect an episode
Enter    Confirm
Esc/q    Quit
```

The downloader reads Firefox cookies from the active profile and uses them for
AnimePahe and Kwik access. If Cloudflare presents a new challenge, open both
sites in Firefox, complete verification, and run the downloader again.
