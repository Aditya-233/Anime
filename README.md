# AnimePahe CLI Downloader & Streamer

Lightweight, high-performance CLI tool to search, select, and download anime losslessly from AnimePahe directly to MP4 using `fzf`, HTTP/2 workers, sliding-window chunk fetching, on-the-fly AES decryption, and zero-copy FFmpeg streaming.

---

## Features

- **Interactive Fuzzy Selection:** Fast catalog search and multi-episode batch selection powered by `fzf`.
- **Sliding-Window Concurrency:** Downloads HLS chunks in parallel with a bounded sliding window, eliminating pipe stalls and head-of-line blocking.
- **On-the-Fly AES-128 Decryption:** Seamlessly decrypts encrypted HLS segments in worker memory before streaming to FFmpeg.
- **Direct MP4 Remuxing:** Feeds chunks directly into FFmpeg's `stdin` pipe without intermediate `.ts` files on disk.
- **Background Episode Prefetching:** Resolves stream metadata and keys for the next episode in the background while the current episode downloads.
- **Resilient Retry Mechanism:** Full-jitter exponential backoff on network drops, HTTP 429 rate limits, and server hiccups.
- **Zero-Friction Cookie Extraction:** Automatically reads isolated session cookies directly from your active Firefox profile.

---

## Prerequisites

- **Python:** 3.10 or newer
- **FFmpeg:** Installed and available in `$PATH` (`/usr/bin/ffmpeg`)
- **fzf:** Installed and available in `$PATH` (`/usr/bin/fzf`)

---

## Setup

```bash
# Clone the repository
git clone https://github.com/Aditya-233/Anime.git
cd Anime

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Usage

```bash
# Interactive prompt
python animepahe.py

# Or pass the search query directly as an argument
python animepahe.py "Spy x Family"
```

### Controls

| Key | Action |
| --- | --- |
| `↑` / `↓` | Navigate items |
| `Tab` / `Shift-Tab` | Select / deselect multiple episodes |
| `Enter` | Confirm selection |
| `Esc` / `Ctrl-C` | Exit |

---

## Output

Downloaded videos are saved losslessly to:

```text
~/Downloads/<Anime Title>/<Anime Title> - E<Episode> [<Quality>].mp4
```

---

## Cloudflare / Protection Notice

The downloader extracts cookies directly from the active Firefox profile (`cookies.sqlite`). If AnimePahe or Kwik triggers a Cloudflare challenge, simply open the site once in Firefox, pass the verification, and run the script again.
