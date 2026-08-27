# AnimePahe CLI Downloader & Streamer

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Platform-Linux-FCC624?style=for-the-badge&logo=linux&logoColor=black" alt="Linux" />
  <img src="https://img.shields.io/badge/Code_Style-Ruff-black?style=for-the-badge&logo=ruff&logoColor=white" alt="Ruff" />
  <img src="https://img.shields.io/badge/Type_Checked-Basedpyright-blue?style=for-the-badge" alt="Basedpyright" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="MIT License" />
</p>

<p align="center">
  A lightweight, blazingly fast CLI tool to search, batch-select, and stream anime losslessly from <b>AnimePahe</b> directly to <b>MP4</b> using <code>fzf</code>, HTTP/2 concurrency, sliding-window chunk fetching, on-the-fly AES-128 decryption, and zero-copy FFmpeg streaming.
</p>

---

## ⚡ Preview

```text
🌸 Select Anime > Spy x Family
> [1/8] Spy x Family
  [2/8] Spy x Family Part 2
  [3/8] Spy x Family Season 2
  [4/8] Spy x Family Code: White

🌸 Episodes (Spy x Family) >
> [Tab/Shift-Tab] Select Multiple  |  [Enter] Confirm
> [x] Episode 001
  [x] Episode 002
  [ ] Episode 003

󰇚 [1/2] Spy x Family - E001 [1080p].mp4... [████████████████████] 100% (146/146)
✔ Downloaded: Spy x Family - E001 [1080p].mp4
```

---

## ✨ Features

- **🎯 Interactive Fuzzy Selection:** Rapid catalog search and multi-episode batch selection powered by `fzf`.
- **🚀 Sliding-Window Concurrency:** Downloads HLS chunks in parallel with a dynamic sliding window, eliminating pipe stalls, buffer starvation, and head-of-line blocking.
- **🔐 On-the-Fly AES-128-CBC Decryption:** Decrypts encrypted stream segments directly in worker memory with zero disk overhead.
- **📼 Direct MP4 Pipe Streaming:** Feeds raw video directly into FFmpeg's `stdin` pipe without intermediate `.ts` file generation.
- **⚡ Seamless Background Prefetching:** Resolves stream metadata and keys for the next episode in the background while the current episode downloads.
- **🛡️ Resilient Network Engine:** Full-jitter exponential backoff on network drops, HTTP 429 rate limits, and server glitches.
- **🍪 Zero-Friction Cookie Extraction:** Automatically reads isolated session cookies directly from your active Firefox profile (`cookies.sqlite`) using read-only immutable SQLite connections.

---

## 📦 Prerequisites

Ensure the following tools are installed on your system:

- **Python:** `3.10` or higher
- **FFmpeg:** `/usr/bin/ffmpeg`
- **fzf:** `/usr/bin/fzf`

On Arch Linux:
```bash
sudo pacman -S python ffmpeg fzf
```

---

## 🛠️ Installation

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

## 🎮 Usage

```bash
# Interactive mode
python animepahe.py

# Or pass a search query directly
python animepahe.py "Frieren"
```

### Controls

| Key | Action |
| :--- | :--- |
| `↑` / `↓` | Navigate list |
| `Tab` / `Shift-Tab` | Select / deselect multiple episodes |
| `Enter` | Confirm selection and begin download |
| `Esc` / `Ctrl-C` | Cancel / Exit |

---

## 📂 Output Location

All downloaded videos are losslessly packaged and saved to:

```text
~/Downloads/<Anime Title>/<Anime Title> - E<Episode> [<Quality>].mp4
```

---

## 🛡️ Cloudflare Notice

The downloader extracts cookies directly from the active Firefox profile (`cookies.sqlite`). If AnimePahe or Kwik triggers a Cloudflare challenge, simply open the site once in Firefox, pass the verification, and run the script again.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

