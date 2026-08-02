# AnimePahe 🚀

> An ultra-fast, lightweight, and keyboard-driven AnimePahe downloader & TUI for Linux.

[![Language](https://img.shields.io/badge/language-Rust-orange.svg?style=flat-square)](https://www.rust-lang.org)
[![TUI Framework](https://img.shields.io/badge/TUI-Ratatui%200.30-red.svg?style=flat-square)](https://ratatui.rs)
[![Binary Size](https://img.shields.io/badge/binary--size-3.7_MB-blueviolet.svg?style=flat-square)](#)
[![License](https://img.shields.io/badge/license-MIT%2FApache--2.0-lightgrey.svg?style=flat-square)](#)

**AnimePahe** brings a streamlined, multi-pane terminal interface to searching and downloading anime episodes. Combining concurrent multi-threaded segment fetching, in-place AES-128 stream decryption, and real-time speed monitoring, it delivers an optimized, lightweight console experience built entirely in Rust.

---

## ✨ Key Features

- ⚡ **Concurrent HLS Segment Fetcher:** Parallel `.ts` segment downloads with Tokio async worker pools.
- 🔐 **In-Place AES-128 Decryption:** Seamless decryption for encrypted stream segments.
- 🎨 **Catppuccin Mocha Styling:** Vibrant, readable layout with dedicated panels for search, episodes, downloads, and keybindings.
- 💾 **Persistent Download Cache:** Auto-saves completed and active downloads to `~/.cache/animepahe/history.json`.
- 🛠️ **Automatic FFmpeg Integration:** Transparently merges segments into `.mp4` using FFmpeg (with fallback to direct binary concatenation).

---

## 🎮 Keyboard Controls

| Key         | Action                                                                                                               |
| :---------- | :------------------------------------------------------------------------------------------------------------------- |
| `Tab`       | Cycle focus between panels (`QueryInput` $\rightarrow$ `Results` $\rightarrow$ `Episodes` $\rightarrow$ `Downloads`) |
| `Shift+Tab` | Cycle focus backward                                                                                                 |
| `Enter`     | Trigger search (when query is focused) or select anime / start episode download                                      |
| `↑` / `↓`   | Navigate active table rows                                                                                           |
| `?`         | Toggle keybindings help overlay modal                                                                                |
| `Esc` / `q` | Close help popup or exit application                                                                                 |

---

## 🏗️ Building from Source

```bash
# Build optimized release binary
cargo build --release

# Run binary directly
./animepahe
```
