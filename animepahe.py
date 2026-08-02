#!/usr/bin/env python3
"""Minimal interactive AnimePahe downloader."""

from __future__ import annotations

import curses
from functools import lru_cache
import re
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

try:
    from Crypto.Cipher import AES
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session
    from curl_cffi.requests.exceptions import RequestException
except ImportError as exc:
    raise SystemExit("Missing dependency. Run: .venv/bin/python -m pip install -r requirements.txt") from exc


BASE_URL = "https://animepahe.pw"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
}
DOWNLOADS = Path.home() / "Downloads"
THREADS = 4


@lru_cache(maxsize=1)
def firefox_cookies() -> tuple[dict[str, str], dict[str, str]]:
    root = Path.home() / ".config" / "mozilla" / "firefox"
    databases = list(root.rglob("cookies.sqlite")) if root.exists() else []
    if not databases:
        return {}, {}

    database = max(databases, key=lambda path: path.stat().st_mtime)
    animepahe: dict[str, str] = {}
    kwik: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="animepahe-") as directory:
        copy = Path(directory) / "cookies.sqlite"
        shutil.copy2(database, copy)
        with sqlite3.connect(copy) as connection:
            rows = connection.execute("SELECT host, name, value FROM moz_cookies WHERE host LIKE '%animepahe%' OR host LIKE '%kwik%'")
            for host, name, value in rows:
                if "kwik" in host:
                    kwik[name] = value
                elif "animepahe" in host:
                    animepahe[name] = value
    return animepahe, kwik


def cookie_header(url: str) -> str:
    animepahe, kwik = firefox_cookies()
    hostname = urlparse(url).hostname or ""
    cookies = kwik if "kwik" in hostname.lower() else animepahe
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


class Client:
    def __init__(self) -> None:
        self.session = Session(
            impersonate="firefox133",
            curl_options={CurlOpt.DOH_URL: "https://1.1.1.1/dns-query"},
        )

    def get(self, url: str, referer: str | None = None, raw: bool = False):
        headers = HEADERS.copy()
        cookies = cookie_header(url)
        if cookies:
            headers["Cookie"] = cookies
        if referer:
            headers["Referer"] = referer

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=30,
                    allow_redirects=True,
                    verify=False,
                    discard_cookies=True,
                )
                body = response.content
                body_lower = body.lower()
                challenge = response.status_code == 403 or response.headers.get("cf-mitigated") == "challenge" or b"<title>just a moment" in body_lower or b"cf-chl-widget" in body_lower or b"challenge-error-text" in body_lower
                if challenge:
                    raise RuntimeError("AnimePahe returned a Cloudflare challenge")
                if response.status_code == 200 or raw:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code}: {url}")
            except (RequestException, OSError, RuntimeError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
        raise RuntimeError(str(last_error or "request failed"))


def search(client: Client, query: str) -> list[dict]:
    response = client.get(f"{BASE_URL}/api?m=search&q={quote(query)}", f"{BASE_URL}/")
    return response.json().get("data", [])


def episodes(client: Client, session: str) -> list[dict]:
    result = []
    page = 1
    while True:
        response = client.get(
            f"{BASE_URL}/api?m=release&id={session}&sort=episode_asc&page={page}",
            f"{BASE_URL}/",
        )
        data = response.json()
        current = data.get("data", [])
        if not current:
            return result
        result.extend(current)
        if page >= data.get("last_page", 1):
            return result
        page += 1


def stream_links(client: Client, anime_session: str, episode_session: str) -> list[dict]:
    url = f"{BASE_URL}/play/{anime_session}/{episode_session}"
    html = client.get(url, f"{BASE_URL}/").text
    pattern = r'<(?:a|button)\s+[^>]*(?:href|src|data-src|data-url)=["\']([^"\']*kwik[^"\']*)["\'][^>]*>(.*?)</(?:a|button)>'
    links = []
    for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
        target, label_html = match.groups()
        label = re.sub(r"<[^>]+>", " ", label_html).strip()
        quality_match = re.search(r"(\d{3,4}p)", label or target, re.IGNORECASE)
        links.append(
            {
                "url": target,
                "quality": quality_match.group(1).lower() if quality_match else "unknown",
                "audio": "dub" if "dub" in label.lower() else "sub",
            }
        )
    unique = {link["url"]: link for link in links}
    return list(unique.values())


def choose_stream(links: list[dict]) -> dict:
    preferred = [link for link in links if link["audio"] == "sub"] or links

    def quality_number(link: dict) -> int:
        match = re.search(r"\d+", link["quality"])
        return int(match.group()) if match else 0

    return max(preferred, key=quality_number)


def unpack_js(payload: str, base: int, count: int, words: list[str]) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def encoded(number: int) -> str:
        if number == 0:
            return "0"
        result = ""
        while number:
            result = alphabet[number % base] + result
            number //= base
        return result

    for index in range(count - 1, -1, -1):
        if index < len(words) and words[index]:
            payload = re.sub(r"\b" + re.escape(encoded(index)) + r"\b", words[index], payload)
    return payload


def m3u8_url(client: Client, kwik: str, referer: str) -> str:
    html = client.get(kwik, referer).text
    direct = re.search(r"https?://[^\s\"'<>\\;]+\.m3u8[^\s\"'<>\\;]*", html)
    if direct:
        return direct.group().rstrip("\\'\"")
    packed = re.compile(r"\}\s*\('(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'", re.DOTALL)
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE):
        for match in packed.finditer(script):
            decoded = unpack_js(match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).split("|"))
            direct = re.search(r"https?://[^\s\"'<>\\;]+\.m3u8[^\s\"'<>\\;]*", decoded)
            if direct:
                return direct.group().rstrip("\\'\"")
    raise RuntimeError("Could not resolve the stream URL")


def parse_playlist(text: str, playlist_url: str) -> tuple[list[str], bytes | None, bytes | None]:
    base = playlist_url.rsplit("/", 1)[0] + "/"
    segments: list[str] = []
    key_url = None
    iv = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            uri = re.search(r'URI="([^"]+)"', line)
            if uri:
                key_url = urljoin(base, uri.group(1))
            iv_match = re.search(r"IV=0x([0-9a-fA-F]+)", line)
            if iv_match:
                iv = bytes.fromhex(iv_match.group(1).zfill(32))
        elif line and not line.startswith("#"):
            segments.append(urljoin(base, line))
    return segments, key_url.encode() if key_url else None, iv


def select_variant(client: Client, url: str, text: str, referer: str) -> tuple[str, str]:
    if "#EXT-X-STREAM-INF" not in text:
        return url, text
    best_bandwidth = -1
    best_url = url
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if "#EXT-X-STREAM-INF" not in line:
            continue
        bandwidth_match = re.search(r"BANDWIDTH=(\d+)", line)
        bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
        if bandwidth > best_bandwidth:
            best_bandwidth = bandwidth
            best_url = urljoin(url, lines[index + 1].strip())
    return best_url, client.get(best_url, referer).text


def download(client: Client, url: str, output: Path, referer: str, progress) -> None:
    playlist = client.get(url, referer).text
    playlist_url, playlist = select_variant(client, url, playlist, referer)
    segments, key_url, iv = parse_playlist(playlist, playlist_url)
    if not segments:
        raise RuntimeError("Playlist has no segments")
    key = client.get(key_url.decode(), referer).content[:16] if key_url else None
    if key is not None and len(key) != 16:
        raise RuntimeError("Invalid AES key")

    def fetch(item: tuple[int, str]) -> tuple[int, bytes]:
        index, segment_url = item
        data = client.get(segment_url, referer).content
        if not data or data.startswith((b"<!DOCTYPE", b"<html")):
            raise RuntimeError("segment response was not media")
        if key:
            data = AES.new(key, AES.MODE_CBC, iv or b"\0" * 16).decrypt(data[: len(data) // 16 * 16])
        return index, data

    chunks: dict[int, bytes] = {}
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(fetch, item) for item in enumerate(segments)]
        for future in as_completed(futures):
            index, data = future.result()
            chunks[index] = data
            progress()

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    with partial.open("wb") as destination:
        for index in range(len(segments)):
            destination.write(chunks[index])
    partial.replace(output)


def display(stdscr, title: str, items: list[str], multi: bool = False) -> list[int] | None:
    curses.curs_set(0)
    selected: set[int] = set()
    cursor = 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD)
        visible = max(1, height - 3)
        start = max(0, min(cursor - visible // 2, len(items) - visible))
        for row, index in enumerate(range(start, min(len(items), start + visible)), 2):
            marker = ">" if index == cursor else " "
            check = "[x] " if index in selected else "[ ] " if multi else ""
            stdscr.addnstr(row, 0, f"{marker} {check}{items[index]}", width - 1)
        stdscr.addnstr(
            height - 1,
            0,
            "Up/Down navigate  Space select  Enter confirm  Esc quit",
            width - 1,
            curses.A_DIM,
        )
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(items) - 1, cursor + 1)
        elif key == ord(" ") and multi:
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif key in (10, 13, curses.KEY_ENTER):
            return sorted(selected) if multi and selected else [cursor]
        elif key in (27, ord("q")):
            return None


def run(stdscr) -> None:
    curses.curs_set(1)
    stdscr.addstr(0, 0, "AnimePahe search: ")
    curses.echo()
    query = stdscr.getstr(0, 18).decode(errors="replace").strip()
    curses.noecho()
    if not query:
        return
    client = Client()
    results = search(client, query)
    if not results:
        raise RuntimeError("No results found")
    result_indexes = display(stdscr, "Select anime", [item.get("title", "Unknown") for item in results])
    if result_indexes is None:
        return
    anime = results[result_indexes[0]]
    anime_episodes = episodes(client, anime["session"])
    if not anime_episodes:
        raise RuntimeError("No episodes found")
    labels = [f"Episode {episode.get('episode', '?')}  {episode.get('title', '')}" for episode in anime_episodes]
    episode_indexes = display(stdscr, anime.get("title", "Episodes"), labels, multi=True)
    if episode_indexes is None:
        return
    output_dir = DOWNLOADS / safe_name(anime.get("title", "Anime"))
    for number, index in enumerate(episode_indexes, 1):
        episode = anime_episodes[index]
        links = stream_links(client, anime["session"], episode["session"])
        if not links:
            raise RuntimeError(f"No stream found for episode {episode.get('episode')}")
        stream = choose_stream(links)
        playlist = m3u8_url(client, stream["url"], f"{BASE_URL}/play/{anime['session']}/{episode['session']}")
        filename = f"{safe_name(anime.get('title', 'Anime'))} - E{episode_number(episode.get('episode'))} [{stream['quality']}].ts"
        target = output_dir / filename
        if target.exists():
            continue
        done = 0

        def progress(number=number, filename=filename) -> None:
            nonlocal done
            done += 1
            print(
                f"\rDownloading {number}/{len(episode_indexes)}: {filename} ({done} segments)",
                end="",
                flush=True,
            )

        download(client, playlist, target, stream["url"], progress)
        print(f"\nDownloaded {target}")


def episode_number(value) -> str:
    try:
        number = float(value)
        return f"{int(number):03d}" if number.is_integer() else str(value)
    except (TypeError, ValueError):
        return str(value)


def safe_name(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", value).strip() or "Anime"


def main() -> None:
    try:
        curses.wrapper(run)
    except RuntimeError as error:
        print(f"Error: {error}")
    except KeyboardInterrupt:
        print("\nCancelled")


if __name__ == "__main__":
    main()
