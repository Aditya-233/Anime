#!/usr/bin/env python3
"""Aesthetically pleasing interactive AnimePahe downloader for Catppuccin / Nerd Font terminals."""

from __future__ import annotations

import curses
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
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
THREADS = 16

# Pre-compiled regex patterns for maximum performance
KWIK_LINK_RE = re.compile(
    r'<(?:a|button)\s+[^>]*(?:href|src|data-src|data-url)=["\']([^"\']*kwik[^"\']*)["\'][^>]*>(.*?)</(?:a|button)>',
    re.IGNORECASE | re.DOTALL,
)
QUALITY_RE = re.compile(r"(\d{3,4}p)", re.IGNORECASE)
PACKED_JS_RE = re.compile(r"\}\s*\('(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'", re.DOTALL)
DIRECT_M3U8_RE = re.compile(r"https?://[^\s\"'<>\\;]+\.m3u8[^\s\"'<>\\;]*")
TAG_STRIP_RE = re.compile(r"<[^>]+>")
DIGITS_RE = re.compile(r"\D")
SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')
VARIANT_RE = re.compile(r"#EXT-X-STREAM-INF:.*BANDWIDTH=(\d+).*\n([^\s#]+)")
KEY_URI_RE = re.compile(r'URI="([^"]+)"')
KEY_IV_RE = re.compile(r"IV=0x([0-9a-fA-F]+)")


@lru_cache(maxsize=1)
def firefox_cookies() -> tuple[dict[str, str], dict[str, str]]:
    root = Path.home() / ".config" / "mozilla" / "firefox"
    databases = list(root.rglob("cookies.sqlite")) if root.exists() else []
    if not databases:
        return {}, {}

    database = max(databases, key=lambda path: path.stat().st_mtime)
    animepahe: dict[str, str] = {}
    kwik: dict[str, str] = {}
    with sqlite3.connect(f"file:{database}?immutable=1", uri=True) as connection:
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

    def get(self, url: str, referer: str | None = None):
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
                if response.status_code == 200:
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
    first = client.get(
        f"{BASE_URL}/api?m=release&id={session}&sort=episode_asc&page=1",
        f"{BASE_URL}/",
    ).json()
    data = first.get("data", [])
    last_page = first.get("last_page", 1)
    if last_page <= 1:
        return data

    pages_data: dict[int, list] = {1: data}

    def fetch_page(page_num: int):
        resp = client.get(
            f"{BASE_URL}/api?m=release&id={session}&sort=episode_asc&page={page_num}",
            f"{BASE_URL}/",
        ).json()
        return page_num, resp.get("data", [])

    with ThreadPoolExecutor(max_workers=min(8, last_page - 1)) as pool:
        futures = [pool.submit(fetch_page, p) for p in range(2, last_page + 1)]
        for future in as_completed(futures):
            p_num, p_data = future.result()
            pages_data[p_num] = p_data

    result = []
    for p in range(1, last_page + 1):
        result.extend(pages_data.get(p, []))
    return result


def stream_links(client: Client, anime_session: str, episode_session: str) -> list[dict]:
    url = f"{BASE_URL}/play/{anime_session}/{episode_session}"
    html = client.get(url, f"{BASE_URL}/").text
    links = []
    for match in KWIK_LINK_RE.finditer(html):
        target, label_html = match.groups()
        label = TAG_STRIP_RE.sub(" ", label_html).strip()
        quality_match = QUALITY_RE.search(label or target)
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
    return max(preferred, key=lambda l: int(DIGITS_RE.sub("", l["quality"]) or 0))


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
    direct = DIRECT_M3U8_RE.search(html)
    if direct:
        return direct.group().rstrip("\\'\"")
    for match in PACKED_JS_RE.finditer(html):
        decoded = unpack_js(match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).split("|"))
        direct = DIRECT_M3U8_RE.search(decoded)
        if direct:
            return direct.group().rstrip("\\'\"")
    raise RuntimeError("Could not resolve the stream URL")


def parse_playlist(text: str, playlist_url: str) -> tuple[list[str], str | None, bytes | None]:
    base = playlist_url.rsplit("/", 1)[0] + "/"
    segments: list[str] = []
    key_url = None
    iv = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            uri = KEY_URI_RE.search(line)
            if uri:
                key_url = urljoin(base, uri.group(1))
            iv_match = KEY_IV_RE.search(line)
            if iv_match:
                iv = bytes.fromhex(iv_match.group(1).zfill(32))
        elif line and not line.startswith("#"):
            segments.append(urljoin(base, line))
    return segments, key_url, iv


def select_variant(client: Client, url: str, text: str, referer: str) -> tuple[str, str]:
    if "#EXT-X-STREAM-INF" not in text:
        return url, text
    variants = VARIANT_RE.findall(text)
    if not variants:
        return url, text
    best_path = max(variants, key=lambda v: int(v[0]))[1]
    best_url = urljoin(url, best_path.strip())
    return best_url, client.get(best_url, referer).text


def download(client: Client, url: str, output: Path, referer: str, progress) -> None:
    playlist = client.get(url, referer).text
    playlist_url, playlist = select_variant(client, url, playlist, referer)
    segments, key_url, iv = parse_playlist(playlist, playlist_url)
    if not segments:
        raise RuntimeError("Playlist has no segments")
    key = client.get(key_url, referer).content[:16] if key_url else None
    if key is not None and len(key) != 16:
        raise RuntimeError("Invalid AES key")

    def fetch(item: tuple[int, str]) -> tuple[int, bytes]:
        index, segment_url = item
        resp = client.session.get(
            segment_url,
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            timeout=30,
            verify=False,
        )
        data = resp.content
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
            progress(len(segments))

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    with partial.open("wb") as destination:
        for index in range(len(segments)):
            destination.write(chunks[index])
    partial.replace(output)


def setup_theme() -> dict[str, int]:
    if not curses.has_colors():
        return {}
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_MAGENTA, -1)
    curses.init_pair(2, curses.COLOR_CYAN, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_MAGENTA)
    return {
        "accent": curses.color_pair(1) | curses.A_BOLD,
        "cyan": curses.color_pair(2) | curses.A_BOLD,
        "green": curses.color_pair(3) | curses.A_BOLD,
        "yellow": curses.color_pair(4) | curses.A_BOLD,
        "highlight": curses.color_pair(5) | curses.A_BOLD,
        "header": curses.color_pair(6) | curses.A_BOLD,
        "dim": curses.A_DIM,
    }


def display(stdscr, title: str, items: list[str], multi: bool = False) -> list[int] | None:
    curses.curs_set(0)
    colors = setup_theme()
    selected: set[int] = set()
    cursor = 0

    accent_attr = colors.get("accent", curses.A_BOLD)
    highlight_attr = colors.get("highlight", curses.A_REVERSE)
    green_attr = colors.get("green", curses.A_BOLD)
    dim_attr = colors.get("dim", curses.A_DIM)

    while True:
        stdscr.erase()
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


def search_modal(stdscr) -> str:
    curses.curs_set(1)
    colors = setup_theme()
    accent_attr = colors.get("accent", curses.A_BOLD)
    cyan_attr = colors.get("cyan", curses.A_BOLD)
    dim_attr = colors.get("dim", curses.A_DIM)

    stdscr.erase()
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
    query_bytes = stdscr.getstr(2, 2 + len(prompt)).strip()
    curses.noecho()
    return query_bytes.decode(errors="replace").strip()


LAST_DIR_FILE = Path("/tmp/.anime_last_dir")


def run(stdscr) -> None:
    if LAST_DIR_FILE.exists():
        try:
            LAST_DIR_FILE.unlink()
        except OSError:
            pass

    query = search_modal(stdscr)
    if not query:
        return
    client = Client()
    results = search(client, query)
    if not results:
        raise RuntimeError("No results found")
    result_indexes = display(stdscr, f"Search Results: '{query}'", [item.get("title", "Unknown") for item in results])
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
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        LAST_DIR_FILE.write_text(str(output_dir))
    except OSError:
        pass

    selected_eps = [anime_episodes[i] for i in episode_indexes]

    for number, ep in enumerate(selected_eps, 1):
        links = stream_links(client, anime["session"], ep["session"])
        if not links:
            raise RuntimeError(f"No stream found for episode {ep.get('episode')}")
        stream = choose_stream(links)
        playlist = m3u8_url(client, stream["url"], f"{BASE_URL}/play/{anime['session']}/{ep['session']}")
        filename = f"{safe_name(anime.get('title', 'Anime'))} - E{episode_number(ep.get('episode'))} [{stream['quality']}].ts"
        target = output_dir / filename
        if target.exists():
            print(f"\033[90m[Skipped]\033[0m {filename} already exists.")
            continue
        done = 0

        def progress(total_segments: int, number=number, filename=filename) -> None:
            nonlocal done
            done += 1
            percent = int((done / total_segments) * 100) if total_segments else 0
            bar_width = 20
            filled = int((done / total_segments) * bar_width) if total_segments else 0
            bar = "█" * filled + "░" * (bar_width - filled)
            disp_name = filename if len(filename) <= 40 else filename[:37] + "..."
            print(
                f"\r\033[K\033[35m󰇚 [{number}/{len(selected_eps)}]\033[0m {disp_name} \033[36m[{bar}]\033[0m \033[1m{percent}%\033[0m ({done}/{total_segments})",
                end="",
                flush=True,
            )

        download(client, playlist, target, stream["url"], progress)
        print(f"\r\033[K\033[32m✔ Downloaded:\033[0m {filename}")


def episode_number(value) -> str:
    try:
        number = float(value)
        return f"{int(number):03d}" if number.is_integer() else str(value)
    except (TypeError, ValueError):
        return str(value)


def safe_name(value: str) -> str:
    return SAFE_NAME_RE.sub("_", value).strip() or "Anime"


def main() -> None:
    try:
        curses.wrapper(run)
    except RuntimeError as error:
        print(f"\033[31mError:\033[0m {error}")
    except KeyboardInterrupt:
        print("\n\033[33mCancelled\033[0m")


if __name__ == "__main__":
    main()
