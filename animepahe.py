#!/usr/bin/env python3
# Copyright (c) 2026 Aditya. All rights reserved.
"""AnimePahe Downloader and Streamer (Hardcoded Arch Linux System Edition).

Lightweight, high-performance CLI tool to search, select, and stream anime
losslessly directly to MP4 using fzf, resilient HTTP/2 workers, and FFmpeg.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, urlparse

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from curl_cffi import CurlOpt
from curl_cffi.requests import Response, Session
from curl_cffi.requests.exceptions import RequestException

if TYPE_CHECKING:
    from Crypto.Cipher._mode_cbc import CbcMode

# Hardcoded System Constants
BASE_URL: str = "https://animepahe.pw"
DOWNLOAD_DIR: Path = Path("/home/aditya/Downloads")
COOKIES_DB: Path = Path("/home/aditya/.config/mozilla/firefox/ay8n5pl1.default-release/cookies.sqlite")
USER_AGENT: str = "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0"
FFMPEG_BIN: str = "/usr/bin/ffmpeg"
FZF_BIN: str = "/usr/bin/fzf"
PARALLEL_WORKERS: int = 6
SLIDING_WINDOW_AHEAD: int = 8
MAX_SEGMENT_RETRIES: int = 10

PACKED_JS_RE: re.Pattern[str] = re.compile(r"\}\s*\(\x27(.*?)\x27\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\x27(.*?)\x27", re.DOTALL)


def get_cookies_map() -> tuple[dict[str, str], dict[str, str]]:
    """Extract isolated session cookies directly from the active Firefox database."""
    pahe: dict[str, str] = {}
    kwik: dict[str, str] = {}
    if not COOKIES_DB.exists():
        return pahe, kwik

    try:
        with sqlite3.connect(f"file:{COOKIES_DB}?mode=ro&immutable=1", uri=True) as conn:
            query = "SELECT host, name, value FROM moz_cookies WHERE host LIKE '%animepahe%' OR host LIKE '%kwik%' ORDER BY lastAccessed ASC"
            rows: list[tuple[str, str, str]] = conn.execute(query).fetchall()
            for host, name, val in rows:
                if "kwik" in host:
                    kwik[name] = val
                elif "animepahe" in host:
                    pahe[name] = val
    except (sqlite3.Error, OSError):
        pass
    return pahe, kwik


class Client:
    """HTTP Client configured with Firefox impersonation, DNS over HTTPS, and cookies."""

    pahe_cookies: dict[str, str]
    kwik_cookies: dict[str, str]
    _local: threading.local

    def __init__(self) -> None:
        """Initialize HTTP client with isolated cookies and ECH/DoH."""
        self.pahe_cookies, self.kwik_cookies = get_cookies_map()
        self._local = threading.local()

    @property
    def session(self) -> Session:
        """Get or initialize thread-local Session to ensure thread-safety."""
        if not hasattr(self._local, "session"):
            self._local.session = Session(
                impersonate="firefox133",
                verify=False,
                curl_options={
                    CurlOpt.DOH_URL: "https://mozilla.cloudflare-dns.com/dns-query",
                    CurlOpt.ECH: "true",
                },
            )
        return self._local.session

    def get(self, url: str, referer: str = f"{BASE_URL}/") -> Response:
        """Send an authenticated GET request with the appropriate domain headers."""
        host = urlparse(url).hostname or ""
        cookies = self.kwik_cookies if "kwik" in host else self.pahe_cookies
        headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return self.session.get(url, headers=headers)


def fzf_select(items: list[str], prompt: str, *, multi: bool = False) -> list[str]:
    """Present an interactive fzf modal to choose one or multiple items."""
    cmd: list[str] = [
        FZF_BIN,
        "--prompt",
        f"🌸 {prompt} > ",
        "--height",
        "45%",
        "--layout=reverse",
        "--border",
    ]
    if multi:
        cmd.extend(["-m", "--header", "[Tab/Shift-Tab] Select Multiple  |  [Enter] Confirm"])
    proc = subprocess.run(cmd, input="\n".join(items), text=True, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return [l.strip() for l in proc.stdout.splitlines() if l.strip()]


def unpack_js(payload: str, base: int, count: int, words: list[str]) -> str:
    """Decode Dean Edwards packed JavaScript payloads."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def enc(n: int) -> str:
        res = ""
        while n > 0:
            res = alphabet[n % base] + res
            n //= base
        return res or "0"

    for i in range(count - 1, -1, -1):
        if i < len(words) and words[i]:
            payload = re.sub(r"\b" + re.escape(enc(i)) + r"\b", words[i], payload)
    return payload


def _jitter(attempt: int, cap: float = 2.0, base: float = 0.2) -> float:
    """Full-jitter exponential backoff."""
    return min(cap, base * (1 << attempt)) + random.uniform(0.05, 0.2)


def resolve_stream_metadata(client: Client, anime_session: str, ep_session: str) -> dict[str, object]:
    """Fetch play page, decode player JS, and retrieve M3U8 playlist + AES key with retries."""
    play_html = client.get(f"{BASE_URL}/play/{anime_session}/{ep_session}", referer=f"{BASE_URL}/").text

    kwik_match = (
        re.search(
            r'data-src="([^"]*kwik[^"]*)"[^>]*data-resolution="1080"[^>]*data-audio="jpn"',
            play_html,
        )
        or re.search(
            r'data-src="([^"]*kwik[^"]*)"[^>]*data-resolution="720"[^>]*data-audio="jpn"',
            play_html,
        )
        or re.search(r'data-src="([^"]*kwik[^"]*)"[^>]*data-audio="jpn"', play_html)
        or re.search(r'data-src="([^"]*kwik[^"]*)"', play_html)
    )
    if not kwik_match:
        msg = "No stream link found in play payload."
        raise RuntimeError(msg)

    kwik_url = kwik_match.group(1)
    res_match = re.search(r'data-resolution="(\d+)"', kwik_match.group(0))
    quality = f"{res_match.group(1)}p" if res_match else "1080p"

    embed_html = client.get(kwik_url, referer=f"{BASE_URL}/").text
    m3u8_url: str | None = None
    for match in PACKED_JS_RE.finditer(embed_html):
        decoded = unpack_js(
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4).split("|"),
        )
        url_match = re.search(r"https?://[^\s\"\'\\\\]+\.m3u8[^\s\"\'\\\\]*", decoded)
        if url_match:
            m3u8_url = url_match.group().rstrip("'\"")
            break
    if not m3u8_url:
        msg = "Could not resolve m3u8 playlist URL from player payload."
        raise RuntimeError(msg)

    # Fetch playlist with jittered retries
    playlist_text = ""
    for attempt in range(6):
        resp = client.get(m3u8_url, referer=kwik_url)
        if resp.status_code == 200 and resp.text.startswith("#EXT"):
            playlist_text = resp.text
            break
        time.sleep(_jitter(attempt))

    if not playlist_text:
        msg = "Failed to fetch valid m3u8 playlist after retries."
        raise RuntimeError(msg)

    segments = [line for line in playlist_text.splitlines() if line and not line.startswith("#") and line.startswith("http")]
    key_match = re.search(r'URI="([^"]+)"', playlist_text)
    key: bytes | None = None
    if key_match:
        for attempt in range(6):
            k_resp = client.get(key_match.group(1), referer=kwik_url)
            if k_resp.status_code == 200 and not k_resp.content.startswith(b"<!"):
                key = k_resp.content[:16]
                break
            time.sleep(_jitter(attempt))

    return {
        "kwik_url": kwik_url,
        "m3u8_url": m3u8_url,
        "segments": segments,
        "key": key,
        "quality": quality,
    }


def stream_to_mp4(client: Client, meta: dict[str, object], output_path: Path, progress_label: str) -> None:
    """Download HLS segments with sliding window and stream into FFmpeg without pipe stalls."""
    kwik_url = str(meta.get("kwik_url", "https://kwik.cx/"))
    segments = cast(list[str], meta.get("segments", []))
    key = cast(bytes | None, meta.get("key"))

    total = len(segments)
    if total == 0:
        msg = "No segments found in stream metadata."
        raise RuntimeError(msg)

    temp_mp4 = output_path.with_name(f"{output_path.stem}.part.mp4")

    cmd: list[str] = [
        FFMPEG_BIN,
        "-y",
        "-fflags",
        "+genpts+discardcorrupt",
        "-i",
        "pipe:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-loglevel",
        "error",
        str(temp_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def fetch_segment(item: tuple[int, str]) -> tuple[int, bytes]:
        index, seg_url = item
        seg_iv = index.to_bytes(16, "big")
        headers = {"User-Agent": USER_AGENT, "Referer": kwik_url}
        for attempt in range(MAX_SEGMENT_RETRIES):
            try:
                resp = client.session.get(seg_url, headers=headers, timeout=20)
                if resp.status_code == 200 and resp.content and not resp.content.startswith(b"<!") and not resp.content.startswith(b"<html"):
                    data = resp.content
                    if key:
                        cipher: CbcMode = AES.new(key, AES.MODE_CBC, seg_iv)
                        decrypted = cipher.decrypt(data[: len(data) // 16 * 16])
                        try:
                            data = unpad(decrypted, 16)
                        except ValueError:
                            data = decrypted
                    return index, data
            except (RequestException, OSError):
                pass
            # Full jitter exponential backoff on 429 or server errors
            time.sleep(_jitter(attempt, base=0.15, cap=2.0))

        err_msg = f"Segment {index} failed to download after {MAX_SEGMENT_RETRIES} attempts."
        raise RuntimeError(err_msg)

    ready_chunks: dict[int, bytes] = {}
    next_to_write = 1
    next_to_schedule = 1
    completed_count = 0

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
        active_futures: set[Future[tuple[int, bytes]]] = set()

        # Fill initial sliding window
        while next_to_schedule <= total and next_to_schedule < next_to_write + SLIDING_WINDOW_AHEAD:
            active_futures.add(pool.submit(fetch_segment, (next_to_schedule, segments[next_to_schedule - 1])))
            next_to_schedule += 1

        while active_futures:
            # Wait for at least one segment to complete
            done_set, _ = wait(active_futures, return_when=FIRST_COMPLETED)
            for fut in done_set:
                active_futures.discard(fut)
                idx, chunk = fut.result()
                ready_chunks[idx] = chunk
                completed_count += 1

            # Drain consecutive ordered chunks to FFmpeg stdin
            while next_to_write in ready_chunks:
                data_to_write = ready_chunks.pop(next_to_write)
                if proc.stdin:
                    try:
                        _ = proc.stdin.write(data_to_write)
                    except BrokenPipeError:
                        break
                next_to_write += 1

            # Replenish sliding window dynamically
            while next_to_schedule <= total and next_to_schedule < next_to_write + SLIDING_WINDOW_AHEAD:
                active_futures.add(
                    pool.submit(
                        fetch_segment,
                        (next_to_schedule, segments[next_to_schedule - 1]),
                    )
                )
                next_to_schedule += 1

            pct = int((completed_count / total) * 100) if total else 0
            bar = "█" * (pct // 5) + "░" * (20 - (pct // 5))
            print(
                f"\r\033[K\033[35m󰇚\033[0m {progress_label} \033[36m[{bar}]\033[0m \033[1m{pct}%\033[0m ({completed_count}/{total})",
                end="",
                flush=True,
            )

    if proc.stdin:
        proc.stdin.close()
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        if temp_mp4.exists():
            _ = temp_mp4.unlink(missing_ok=True)
        err_msg = f"FFmpeg failed (code {proc.returncode}): " + f"{stderr.decode(errors='replace').strip()}"
        raise RuntimeError(err_msg)
    if temp_mp4.exists():
        _ = temp_mp4.replace(output_path)
    print(f"\r\033[K\033[32m✔ Downloaded:\033[0m {output_path.name}")


def search_anime(client: Client, query: str) -> list[dict[str, str]]:
    """Search anime catalog by query."""
    resp = client.get(f"{BASE_URL}/api?m=search&q={quote(query)}")
    raw_dict: dict[str, object] = cast(dict[str, object], json.loads(resp.text))
    items = [cast(dict[str, object], x) for x in cast(list[object], raw_dict.get("data") or []) if isinstance(x, dict)]
    return [{"title": str(x.get("title", "Unknown")), "session": str(x.get("session", ""))} for x in items]


def _parse_ep_page(resp_dict: dict[str, object]) -> list[dict[str, str]]:
    """Extract episode list from a single API page response dict."""
    raw = cast(list[object], resp_dict.get("data") or [])
    dicts = [cast(dict[str, object], x) for x in raw if isinstance(x, dict)]
    return [{"episode": str(x.get("episode", "1")), "session": str(x.get("session", ""))} for x in dicts]


def get_episodes(client: Client, anime_session: str) -> list[dict[str, str]]:
    """Fetch all episodes across all release pages."""
    first_resp = client.get(f"{BASE_URL}/api?m=release&id={anime_session}&sort=episode_asc&page=1")
    first_dict: dict[str, object] = cast(dict[str, object], json.loads(first_resp.text))
    result = _parse_ep_page(first_dict)

    last_page = int(str(first_dict.get("last_page", 1))) if str(first_dict.get("last_page", "1")).isdigit() else 1
    for p in range(2, last_page + 1):
        p_resp = client.get(f"{BASE_URL}/api?m=release&id={anime_session}&sort=episode_asc&page={p}")
        result.extend(_parse_ep_page(cast(dict[str, object], json.loads(p_resp.text))))
    return result


def main() -> None:
    """Main entrypoint for searching, selecting, and downloading anime."""
    client = Client()
    query = sys.argv[1] if len(sys.argv) > 1 else input("🔍 Search Anime: ").strip()
    if not query:
        return

    results = search_anime(client, query)
    if not results:
        print(f"\033[31mNo results found for '{query}'\033[0m")
        return

    titles = [item.get("title", "Unknown") for item in results]
    selected_titles = fzf_select(titles, "Select Anime")
    if not selected_titles:
        return
    anime = next(item for item in results if item.get("title") == selected_titles[0])
    anime_title = re.sub(r'[\\/:*?"<>|]', "_", anime.get("title", "Anime"))
    anime_session = anime.get("session", "")
    print(f"\033[36m⏳ Fetching episodes for '{anime.get('title', 'Anime')}'...\033[0m")

    episodes = get_episodes(client, anime_session)
    if not episodes:
        print("\033[31mNo episodes found.\033[0m")
        return

    ep_labels = [f"Episode {ep.get('episode', '0'):>03}".strip() for ep in episodes]
    selected_eps = fzf_select(ep_labels, f"Episodes ({anime.get('title', 'Anime')})", multi=True)
    if not selected_eps:
        return

    chosen = [episodes[ep_labels.index(label)] for label in selected_eps]
    out_dir = DOWNLOAD_DIR / anime_title
    out_dir.mkdir(parents=True, exist_ok=True)

    # Prefetch pipeline executor
    prefetch_pool = ThreadPoolExecutor(max_workers=2)
    next_meta_future: Future[dict[str, object]] | None = None

    try:
        for idx, ep in enumerate(chosen, 1):
            ep_val = str(ep.get("episode", "1"))
            ep_num = f"{float(ep_val):03.0f}" if ep_val.replace(".", "", 1).isdigit() else ep_val
            ep_sess = ep.get("session", "")

            # If this episode was prefetched, retrieve its resolved metadata
            try:
                if next_meta_future is not None:
                    meta = next_meta_future.result()
                    next_meta_future = None
                else:
                    meta = resolve_stream_metadata(client, anime_session, ep_sess)
            except (
                RuntimeError,
                OSError,
                RequestException,
                ValueError,
                KeyError,
            ) as err:
                print(f"\033[31m✘ Failed to resolve stream for Episode {ep_num}: {err}\033[0m")
                next_meta_future = None
                continue

            quality = meta.get("quality", "1080p")
            out_file = out_dir / f"{anime_title} - E{ep_num} [{quality}].mp4"
            if out_file.exists():
                print(f"\033[90m[Skipped]\033[0m {out_file.name} already exists.")
                continue

            # Prefetch the next episode in background while this one downloads
            if idx < len(chosen):
                next_ep_sess = chosen[idx].get("session", "")
                next_meta_future = prefetch_pool.submit(resolve_stream_metadata, client, anime_session, next_ep_sess)

            label = f"[{idx}/{len(chosen)}] {out_file.name[:35]}..."
            try:
                stream_to_mp4(client, meta, out_file, label)
            except (RuntimeError, OSError, RequestException, ValueError) as err:
                print(f"\n\033[31m✘ Failed downloading Episode {ep_num}: {err}\033[0m")
    finally:
        prefetch_pool.shutdown(wait=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\033[33mCancelled\033[0m")
