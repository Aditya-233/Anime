#!/usr/bin/env python3

from __future__ import annotations

import curses
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import quote, urljoin, urlparse

from Crypto.Cipher import AES
from Crypto.Cipher._mode_cbc import CbcMode
from curl_cffi import CurlOpt
from curl_cffi.requests import Response, Session
from curl_cffi.requests.exceptions import RequestException

BASE_URL = "https://animepahe.pw"
DOWNLOADS = Path.home() / "Downloads"

# Pre-compiled regex patterns for maximum performance
KWIK_LINK_RE = re.compile(
    r'<(?:a|button)\s+[^>]*(?:href|src|data-src|data-url)=["\']([^"\']*kwik[^"\']*)["\'][^>]*>(.*?)</(?:a|button)>',
    re.IGNORECASE | re.DOTALL,
)
QUALITY_RE = re.compile(r"(\d{3,4}p)", re.IGNORECASE)
PACKED_JS_RE = re.compile(
    r"\}\s*\('(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'", re.DOTALL
)
DIRECT_M3U8_RE = re.compile(r"https?://[^\s\"'<>\\;]+\.m3u8[^\s\"'<>\\;]*")
TAG_STRIP_RE = re.compile(r"<[^>]+>")
SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')
VARIANT_RE = re.compile(r"#EXT-X-STREAM-INF:.*BANDWIDTH=(\d+).*\n([^\s#]+)")
KEY_URI_RE = re.compile(r'URI="([^"]+)"')
KEY_IV_RE = re.compile(r"IV=0x([0-9a-fA-F]+)")
MEDIA_SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")


class SearchItem(TypedDict, total=False):
    id: int
    title: str
    type: str
    episodes: int
    status: str
    season: str
    year: int
    score: float
    poster: str
    session: str


class EpisodeItem(TypedDict, total=False):
    id: int
    anime_id: int
    episode: float | int | str
    episode2: float | int | str
    edition: str
    title: str
    snapshot: str
    disc: str
    audio: str
    duration: str
    session: str
    filler: int
    created_at: str


class StreamLink(TypedDict):
    url: str
    quality: str
    audio: str


ProgressCallback = Callable[[int], None]


def get_firefox_user_agent(database_path: Path | None = None) -> str:
    version = "154.0"
    if database_path is not None:
        comp_file = database_path.parent / "compatibility.ini"
        if comp_file.exists():
            try:
                content = comp_file.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"LastVersion=([0-9]+)", content)
                if match:
                    version = f"{match.group(1)}.0"
            except OSError:
                pass
    if version == "154.0":
        try:
            res = subprocess.run(
                ["firefox", "--version"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            match = re.search(r"Firefox\s+([0-9]+)", res.stdout)
            if match:
                version = f"{match.group(1)}.0"
        except (subprocess.SubprocessError, OSError):
            pass
    return f"Mozilla/5.0 (X11; Linux x86_64; rv:{version}) Gecko/20100101 Firefox/{version}"


def firefox_cookies() -> tuple[dict[str, str], dict[str, str]]:
    roots: list[Path] = [
        Path.home() / ".config" / "mozilla" / "firefox",
        Path.home() / ".mozilla" / "firefox",
        Path.home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        Path.home()
        / ".var"
        / "app"
        / "org.mozilla.firefox"
        / "data"
        / "mozilla"
        / "firefox",
    ]
    databases: list[Path] = []
    for root in roots:
        if root.exists():
            databases.extend(root.rglob("cookies.sqlite"))
    if not databases:
        return {}, {}

    def get_mtime(path: Path) -> float:
        return path.stat().st_mtime

    database = max(databases, key=get_mtime)
    animepahe: dict[str, str] = {}
    kwik: dict[str, str] = {}

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "cookies.sqlite"
            _ = shutil.copy2(database, tmp_db)
            wal = database.with_name(database.name + "-wal")
            shm = database.with_name(database.name + "-shm")
            if wal.exists():
                _ = shutil.copy2(wal, Path(tmpdir) / wal.name)
            if shm.exists():
                _ = shutil.copy2(shm, Path(tmpdir) / shm.name)

            with sqlite3.connect(tmp_db) as connection:
                rows: list[tuple[str, str, str]] = connection.execute(
                    "SELECT host, name, value FROM moz_cookies WHERE host LIKE '%animepahe%' OR host LIKE '%kwik%'"
                ).fetchall()
                for host, name, value in rows:
                    if "kwik" in host:
                        kwik[name] = value
                    elif "animepahe" in host:
                        animepahe[name] = value
    except (sqlite3.Error, OSError):
        try:
            with sqlite3.connect(
                f"file:{database}?immutable=1", uri=True
            ) as connection:
                fallback_rows: list[tuple[str, str, str]] = connection.execute(
                    "SELECT host, name, value FROM moz_cookies WHERE host LIKE '%animepahe%' OR host LIKE '%kwik%'"
                ).fetchall()
                for host, name, value in fallback_rows:
                    if "kwik" in host:
                        kwik[name] = value
                    elif "animepahe" in host:
                        animepahe[name] = value
        except (sqlite3.Error, OSError):
            pass

    return animepahe, kwik


def cookie_header(url: str) -> str:
    animepahe, kwik = firefox_cookies()
    hostname = urlparse(url).hostname or ""
    cookies = kwik if "kwik" in hostname.lower() else animepahe
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def refresh_browser_cookies() -> None:
    try:
        proc = subprocess.Popen(
            ["firefox", "https://animepahe.pw", "https://kwik.cx"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(5)
        proc.terminate()
    except Exception:
        pass


class CloudflareChallengeError(RuntimeError):
    pass


class Client:
    session: Session
    user_agent: str

    def __init__(self, user_agent: str | None = None) -> None:
        self.session = Session(
            impersonate="firefox133",
            curl_options={
                CurlOpt.DOH_URL: "https://mozilla.cloudflare-dns.com/dns-query",
                CurlOpt.ECH: "true",
            },
        )
        self.user_agent = user_agent or get_firefox_user_agent()

    def get(self, url: str, referer: str | None = None) -> Response:
        headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            headers["Referer"] = referer

        last_error: Exception | None = None
        for attempt in range(4):
            cookies = cookie_header(url)
            if cookies:
                headers["Cookie"] = cookies
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=15,
                    allow_redirects=True,
                    verify=False,
                    discard_cookies=True,
                )
                body = response.content
                body_lower = body.lower()
                is_cf_challenge = (
                    response.headers.get("cf-mitigated") == "challenge"
                    or b"<title>just a moment" in body_lower
                    or b"cf-chl-widget" in body_lower
                    or b"challenge-error-text" in body_lower
                )
                if is_cf_challenge:
                    if attempt < 2:
                        refresh_browser_cookies()
                        continue
                    raise CloudflareChallengeError("Cloudflare verification required.")
                if response.status_code == 200:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code}: {url}")
            except CloudflareChallengeError:
                raise
            except (RequestException, OSError, RuntimeError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(str(last_error or "request failed"))


def search(client: Client, query: str) -> list[SearchItem]:
    response = client.get(f"{BASE_URL}/api?m=search&q={quote(query)}", f"{BASE_URL}/")
    data_dict: dict[str, object] = cast(dict[str, object], json.loads(response.text))
    raw_items: object = data_dict.get("data")
    if isinstance(raw_items, list):
        raw_list: list[object] = cast(list[object], raw_items)
        items: list[dict[str, object]] = [
            cast(dict[str, object], x) for x in raw_list if isinstance(x, dict)
        ]
        results: list[SearchItem] = []
        for item in items:
            title_val = item.get("title")
            session_val = item.get("session")
            results.append(
                {
                    "title": str(title_val) if title_val is not None else "Unknown",
                    "session": str(session_val) if session_val is not None else "",
                }
            )
        return results
    return []


def parse_episode_item(item: dict[str, object]) -> EpisodeItem:
    ep_val = item.get("episode")
    title_val = item.get("title")
    session_val = item.get("session")
    return {
        "episode": str(ep_val) if ep_val is not None else "?",
        "title": str(title_val) if title_val is not None else "",
        "session": str(session_val) if session_val is not None else "",
    }


def episodes(client: Client, session: str) -> list[EpisodeItem]:
    first_resp: dict[str, object] = cast(
        dict[str, object],
        json.loads(
            client.get(
                f"{BASE_URL}/api?m=release&id={session}&sort=episode_asc&page=1",
                f"{BASE_URL}/",
            ).text
        ),
    )
    raw_data = first_resp.get("data")
    data: list[EpisodeItem] = []
    if isinstance(raw_data, list):
        raw_list: list[object] = cast(list[object], raw_data)
        items: list[dict[str, object]] = [
            cast(dict[str, object], x) for x in raw_list if isinstance(x, dict)
        ]
        for item in items:
            data.append(parse_episode_item(item))

    last_page_val = first_resp.get("last_page", 1)
    last_page: int = (
        int(str(last_page_val))
        if isinstance(last_page_val, (int, float, str)) and str(last_page_val).isdigit()
        else 1
    )
    if last_page <= 1:
        return data

    pages_data: dict[int, list[EpisodeItem]] = {1: data}

    def fetch_page(page_num: int) -> tuple[int, list[EpisodeItem]]:
        resp_dict: dict[str, object] = cast(
            dict[str, object],
            json.loads(
                client.get(
                    f"{BASE_URL}/api?m=release&id={session}&sort=episode_asc&page={page_num}",
                    f"{BASE_URL}/",
                ).text
            ),
        )
        page_items = resp_dict.get("data")
        ep_items: list[EpisodeItem] = []
        if isinstance(page_items, list):
            page_list: list[object] = cast(list[object], page_items)
            items_list: list[dict[str, object]] = [
                cast(dict[str, object], x) for x in page_list if isinstance(x, dict)
            ]
            for item in items_list:
                ep_items.append(parse_episode_item(item))
        return page_num, ep_items

    with ThreadPoolExecutor(max_workers=min(8, last_page - 1)) as pool:
        futures = [pool.submit(fetch_page, p) for p in range(2, last_page + 1)]
        for future in as_completed(futures):
            p_num, p_data = future.result()
            pages_data[p_num] = p_data

    result: list[EpisodeItem] = []
    for p in range(1, last_page + 1):
        result.extend(pages_data.get(p, []))
    return result


def stream_links(
    client: Client, anime_session: str, episode_session: str
) -> list[StreamLink]:
    url = f"{BASE_URL}/play/{anime_session}/{episode_session}"
    html = client.get(url, f"{BASE_URL}/").text
    links: list[StreamLink] = []
    for match in KWIK_LINK_RE.finditer(html):
        target, label_html = match.groups()
        label = TAG_STRIP_RE.sub(" ", label_html).strip()
        quality_match = QUALITY_RE.search(label or target)
        quality_val = quality_match.group(1).lower() if quality_match else "unknown"
        audio_val = "dub" if "dub" in label.lower() else "sub"
        links.append(
            {
                "url": target,
                "quality": quality_val,
                "audio": audio_val,
            }
        )
    unique: dict[str, StreamLink] = {link["url"]: link for link in links}
    return list(unique.values())


def choose_stream(links: list[StreamLink]) -> StreamLink:
    preferred = [link for link in links if link["audio"] == "sub"] or links

    def stream_quality_score(item: StreamLink) -> int:
        digits = "".join(filter(str.isdigit, item["quality"]))
        return int(digits) if digits else 0

    return max(preferred, key=stream_quality_score)


def unpack_js(payload: str, base: int, count: int, words: list[str]) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def encoded(number: int) -> str:
        if number == 0:
            return "0"
        result = ""
        while number > 0:
            result = alphabet[number % base] + result
            number //= base
        return result

    for index in range(count - 1, -1, -1):
        if index < len(words) and words[index]:
            payload = re.sub(
                r"\b" + re.escape(encoded(index)) + r"\b", words[index], payload
            )
    return payload


def m3u8_url(client: Client, kwik: str, referer: str) -> str:
    html = client.get(kwik, referer).text
    direct = DIRECT_M3U8_RE.search(html)
    if direct:
        return direct.group().rstrip("\\'\"")
    for match in PACKED_JS_RE.finditer(html):
        decoded = unpack_js(
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4).split("|"),
        )
        direct = DIRECT_M3U8_RE.search(decoded)
        if direct:
            return direct.group().rstrip("\\'\"")
    raise RuntimeError("Could not resolve the stream URL")


def parse_playlist(
    text: str, playlist_url: str
) -> tuple[list[str], str | None, bytes | None, int]:
    segments: list[str] = []
    key_url: str | None = None
    iv: bytes | None = None
    media_seq_match = MEDIA_SEQ_RE.search(text)
    media_sequence = int(media_seq_match.group(1)) if media_seq_match else 1
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            uri = KEY_URI_RE.search(line)
            if uri:
                key_url = urljoin(playlist_url, uri.group(1))
            iv_match = KEY_IV_RE.search(line)
            if iv_match:
                iv = bytes.fromhex(iv_match.group(1).zfill(32))
        elif line and not line.startswith("#"):
            segments.append(urljoin(playlist_url, line))
    return segments, key_url, iv, media_sequence


def select_variant(
    client: Client, url: str, text: str, referer: str
) -> tuple[str, str]:
    if "#EXT-X-STREAM-INF" not in text:
        return url, text
    variants: list[tuple[str, str]] = VARIANT_RE.findall(text)
    if not variants:
        return url, text
    best_path = max(variants, key=lambda v: int(v[0]))[1]
    best_url = urljoin(url, best_path.strip())
    return best_url, client.get(best_url, referer).text


def download(
    client: Client, url: str, output: Path, referer: str, progress: ProgressCallback
) -> None:
    playlist = client.get(url, referer).text
    playlist_url, playlist = select_variant(client, url, playlist, referer)
    segments, key_url, iv, media_sequence = parse_playlist(playlist, playlist_url)
    if not segments:
        raise RuntimeError("Playlist has no segments")
    key: bytes | None = client.get(key_url, referer).content[:16] if key_url else None
    if key is not None and len(key) != 16:
        raise RuntimeError("Invalid AES key")

    rate_limit_lock = threading.Lock()

    def fetch(item: tuple[int, str]) -> tuple[int, bytes]:
        index, segment_url = item
        seg_iv: bytes = (
            iv if iv is not None else (media_sequence + index).to_bytes(16, "big")
        )
        last_err: Exception | None = None
        for attempt in range(15):
            try:
                with rate_limit_lock:
                    time.sleep(0.08)
                resp = client.session.get(
                    segment_url,
                    headers={"User-Agent": client.user_agent, "Referer": referer},
                    timeout=25,
                    verify=False,
                )
                data: bytes = resp.content
                if (
                    not data
                    or resp.status_code != 200
                    or b"<!DOCTYPE" in data[:100].upper()
                    or b"<HTML" in data[:100].upper()
                ):
                    with rate_limit_lock:
                        time.sleep(10.0 * (attempt + 1))
                        client.session = Client(user_agent=client.user_agent).session
                    raise RuntimeError("segment response was not media")

                if key is not None:
                    cipher: CbcMode = AES.new(key, AES.MODE_CBC, seg_iv)
                    data = cipher.decrypt(data[: len(data) // 16 * 16])
                    pad_len = data[-1]
                    if (
                        1 <= pad_len <= 16
                        and data[-pad_len:] == bytes([pad_len]) * pad_len
                    ):
                        data = data[:-pad_len]
                return index, data
            except (RequestException, OSError, RuntimeError, ValueError) as exc:
                last_err = exc
                with rate_limit_lock:
                    time.sleep(float(attempt + 1))
        raise RuntimeError(f"Segment {index} failed after 15 retries: {last_err}")

    chunks: dict[int, bytes] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch, item) for item in enumerate(segments)]
        for future in as_completed(futures):
            index, data = future.result()
            chunks[index] = data
            progress(len(segments))

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    with partial.open("wb") as destination:
        for index in range(len(segments)):
            _ = destination.write(chunks[index])
    _ = partial.replace(output)


def setup_theme() -> dict[str, int]:
    if not curses.has_colors():
        return {}
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_MAGENTA, -1)
    curses.init_pair(2, curses.COLOR_CYAN, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
    return {
        "accent": curses.color_pair(1) | curses.A_BOLD,
        "cyan": curses.color_pair(2) | curses.A_BOLD,
        "green": curses.color_pair(3) | curses.A_BOLD,
        "highlight": curses.color_pair(5) | curses.A_BOLD,
        "dim": curses.A_DIM,
    }


def display(
    stdscr: curses.window, title: str, items: list[str], multi: bool = False
) -> list[int] | None:
    _ = curses.curs_set(0)
    colors = setup_theme()
    selected: set[int] = set()
    cursor = 0

    accent_attr = colors.get("accent", curses.A_BOLD)
    highlight_attr = colors.get("highlight", curses.A_REVERSE)
    green_attr = colors.get("green", curses.A_BOLD)
    dim_attr = colors.get("dim", curses.A_DIM)

    while True:
        stdscr.erase()
        height: int
        width: int
        height, width = stdscr.getmaxyx()
        if height < 6 or width < 20:
            return None

        header_text = f" 🌸 {title} "
        stdscr.attron(accent_attr)
        stdscr.addnstr(0, 0, header_text.ljust(width - 1), width - 1)
        stdscr.attroff(accent_attr)

        visible = max(1, height - 4)
        start = max(0, min(cursor - visible // 2, max(0, len(items) - visible)))

        for row, index in enumerate(range(start, min(len(items), start + visible)), 2):
            is_active = index == cursor
            is_checked = index in selected

            marker = "▶ " if is_active else "  "
            if multi:
                check_str = "[✓] " if is_checked else "[ ] "
            else:
                check_str = ""

            item_str = f"{marker}{check_str}{items[index]}"
            line_str = item_str.ljust(width - 2)

            if is_active:
                stdscr.attron(highlight_attr)
                stdscr.addnstr(row, 1, line_str, width - 2)
                stdscr.attroff(highlight_attr)
            else:
                if is_checked:
                    stdscr.attron(green_attr)
                    stdscr.addnstr(row, 1, line_str, width - 2)
                    stdscr.attroff(green_attr)
                else:
                    stdscr.addnstr(row, 1, line_str, width - 2)

        if multi:
            footer = "  [↑/k] Up  [↓/j] Down  [Space] Select  [a] All  [n] None  [Enter] Confirm  [q] Quit"
        else:
            footer = "  [↑/k] Up  [↓/j] Down  [Enter] Select  [q] Quit"

        stdscr.attron(dim_attr)
        stdscr.addnstr(height - 1, 0, footer.ljust(width - 1), width - 1)
        stdscr.attroff(dim_attr)

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
        elif multi and key in (ord("a"), ord("A")):
            selected = set(range(len(items)))
        elif multi and key in (ord("n"), ord("N")):
            selected.clear()
        elif key in (10, 13, curses.KEY_ENTER):
            return sorted(selected) if multi and selected else [cursor]
        elif key in (27, ord("q")):
            return None


def search_modal(stdscr: curses.window) -> str:
    _ = curses.curs_set(1)
    colors = setup_theme()
    accent_attr = colors.get("accent", curses.A_BOLD)
    cyan_attr = colors.get("cyan", curses.A_BOLD)
    dim_attr = colors.get("dim", curses.A_DIM)

    stdscr.erase()
    height: int
    width: int
    height, width = stdscr.getmaxyx()

    title = " 🔍 AnimePahe Search "
    stdscr.attron(accent_attr)
    stdscr.addnstr(0, 0, title.ljust(width - 1), width - 1)
    stdscr.attroff(accent_attr)

    prompt = " Enter anime name: "
    stdscr.attron(cyan_attr)
    stdscr.addnstr(2, 2, prompt, width - 4)
    stdscr.attroff(cyan_attr)

    footer = " [Enter] Search   [Esc/Ctrl+C] Quit"
    stdscr.attron(dim_attr)
    stdscr.addnstr(height - 1, 0, footer.ljust(width - 1), width - 1)
    stdscr.attroff(dim_attr)

    stdscr.refresh()
    curses.echo()
    query_bytes: bytes = stdscr.getstr(2, 2 + len(prompt)).strip()
    curses.noecho()
    return query_bytes.decode(errors="replace").strip()


def show_status(stdscr: curses.window, message: str) -> None:
    stdscr.erase()
    height: int
    width: int
    height, width = stdscr.getmaxyx()
    colors = setup_theme()
    cyan_attr = colors.get("cyan", curses.A_BOLD)
    stdscr.attron(cyan_attr)
    stdscr.addnstr(height // 2, max(0, (width - len(message)) // 2), message, width - 1)
    stdscr.attroff(cyan_attr)
    stdscr.refresh()


def convert_to_mp4(ts_path: Path, mp4_path: Path) -> None:
    if shutil.which("ffmpeg") is not None:
        cmd: list[str] = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(ts_path),
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(mp4_path),
        ]
        try:
            _ = subprocess.run(cmd, check=True)
            ts_path.unlink(missing_ok=True)
            return
        except subprocess.SubprocessError:
            pass
    if ts_path != mp4_path and ts_path.exists():
        _ = ts_path.replace(mp4_path)


def run(stdscr: curses.window) -> None:
    query = search_modal(stdscr)
    if not query:
        return
    client = Client()
    show_status(stdscr, f"🔍 Searching for '{query}'...")
    results = search(client, query)
    if not results:
        raise RuntimeError("No results found")
    titles = [item.get("title", "Unknown") for item in results]
    result_indexes = display(stdscr, f"Search Results: '{query}'", titles)
    if result_indexes is None:
        return
    anime = results[result_indexes[0]]
    anime_title = anime.get("title", "Anime")
    anime_session = anime.get("session", "")
    show_status(stdscr, f"⏳ Fetching episode list for '{anime_title}'...")
    anime_episodes = episodes(client, anime_session)
    if not anime_episodes:
        raise RuntimeError("No episodes found")
    labels = [
        f"Episode {episode.get('episode', '?')}  {episode.get('title', '')}"
        for episode in anime_episodes
    ]
    episode_indexes = display(stdscr, anime_title, labels, multi=True)
    if episode_indexes is None:
        return
    output_dir = DOWNLOADS / safe_name(anime_title)
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_eps = [anime_episodes[i] for i in episode_indexes]

    for number, ep in enumerate(selected_eps, 1):
        ep_session = ep.get("session", "")
        links = stream_links(client, anime_session, ep_session)
        if not links:
            raise RuntimeError(f"No stream found for episode {ep.get('episode')}")
        stream = choose_stream(links)
        playlist = m3u8_url(
            client, stream["url"], f"{BASE_URL}/play/{anime_session}/{ep_session}"
        )
        base_name = f"{safe_name(anime_title)} - E{episode_number(ep.get('episode'))} [{stream['quality']}]"
        target_mp4 = output_dir / f"{base_name}.mp4"
        target_ts = output_dir / f"{base_name}.ts"
        if target_mp4.exists():
            print(f"\033[90m[Skipped]\033[0m {target_mp4.name} already exists.")
            continue
        if target_ts.exists():
            convert_to_mp4(target_ts, target_mp4)
            print(f"\033[32m✔ Converted to MP4:\033[0m {target_mp4.name}")
            continue

        done = 0

        def progress(
            total_segments: int,
            cur_number: int = number,
            cur_filename: str = target_mp4.name,
        ) -> None:
            nonlocal done
            done += 1
            percent = int((done / total_segments) * 100) if total_segments else 0
            bar_width = 20
            filled = int((done / total_segments) * bar_width) if total_segments else 0
            bar = "█" * filled + "░" * (bar_width - filled)
            disp_name = (
                cur_filename if len(cur_filename) <= 40 else cur_filename[:37] + "..."
            )
            print(
                f"\r\033[K\033[35m󰇚 [{cur_number}/{len(selected_eps)}]\033[0m {disp_name} \033[36m[{bar}]\033[0m \033[1m{percent}%\033[0m ({done}/{total_segments})",
                end="",
                flush=True,
            )

        download(client, playlist, target_ts, stream["url"], progress)
        convert_to_mp4(target_ts, target_mp4)
        print(f"\r\033[K\033[32m✔ Downloaded:\033[0m {target_mp4.name}")


def episode_number(value: object) -> str:
    try:
        number = float(str(value))
        return f"{int(number):03d}" if number.is_integer() else str(value)
    except (TypeError, ValueError):
        return str(value)


def safe_name(value: object) -> str:
    return SAFE_NAME_RE.sub("_", str(value)).strip() or "Anime"


def main() -> None:
    try:
        _ = curses.wrapper(run)
    except RuntimeError as error:
        print(f"\033[31mError:\033[0m {error}")
    except KeyboardInterrupt:
        print("\n\033[33mCancelled\033[0m")


if __name__ == "__main__":
    main()
