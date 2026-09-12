#!/usr/bin/env python3
"""Build full-text RSS feeds for selected Royal Road fiction.

Royal Road's syndication feeds provide the latest chapter metadata, but their
bodies end in ``(...)``.  This builder uses those feeds for discovery, fetches
only missing chapter pages, and extracts the complete ``.chapter-content`` HTML.

Architecture & Pipeline Overview:
  +--------------------------+      +--------------------------+
  |  Syndication RSS Feed    | ---> |  Parse Metadata Listing  |
  |  (/fiction/syndication/) |      |  (titles, links, dates)  |
  +--------------------------+      +--------------------------+
                                                  |
                                                  v
  +--------------------------+      +--------------------------+
  |  Cached History          | ---> |  Differential Merge      |
  |  (local XML / remote)    |      |  (identify new chapters) |
  +--------------------------+      +--------------------------+
                                                  |
                                                  v
                                    +--------------------------+
                                    |  Fetch Full Chapter HTML |
                                    |  (strip watermarks, CSS) |
                                    +--------------------------+
                                                  |
                                                  v
                                    +--------------------------+
                                    |  Write feed.xml & index  |
                                    +--------------------------+

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.
"""
from __future__ import annotations

import datetime as dt
from email.utils import format_datetime, parsedate_to_datetime
from enum import Enum, auto
import html
from http import HTTPStatus
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup
import requests


BASE = "https://www.royalroad.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.7922.76 Safari/537.36"
)
UTC = dt.timezone.utc
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

FEEDS = [
    {
        "key": "zenith-of-sorcery",
        "fiction_id": "71045",
        "slug": "zenith-of-sorcery",
        "title": "Zenith of Sorcery",
        "author": "nobody103",
    },
]

ITEM_LIMIT = max(1, int(os.environ.get("ROYALROAD_MAX_ITEMS", "10")))
TIMEOUT = max(1, int(os.environ.get("ROYALROAD_TIMEOUT", "30")))
RETRIES = max(0, int(os.environ.get("ROYALROAD_RETRIES", "2")))
REQUEST_DELAY = max(0.0, float(os.environ.get("ROYALROAD_REQUEST_DELAY", "1")))
OUT_DIR = Path(os.environ.get("ROYALROAD_OUT_DIR", "public"))
SITE_BASE_URL = os.environ.get("ROYALROAD_SITE_BASE_URL", "").strip().rstrip("/")

DROP_TAGS = {"script", "style", "iframe", "object", "embed", "template", "form"}
ALLOWED_ATTRS = {
    "alt", "colspan", "datetime", "height", "href", "lang", "loading", "rel",
    "rowspan", "src", "title", "width",
}
URL_ATTRS = {"href", "src"}


class FetchMode(Enum):
    """Resource retrieval strictness modes.

    Attributes:
        REQUIRED: Raise an exception if the resource is missing or returns non-200.
        OPTIONAL: Return None if the server returns HTTP 404 (e.g. absent remote feed).
    """

    REQUIRED = auto()
    OPTIONAL = auto()


def make_session() -> requests.Session:
    """Create a configured requests.Session for Royal Road HTTP interactions.

    Returns:
        A requests.Session pre-configured with a browser User-Agent and English
        Accept-Language headers to ensure consistent HTML responses.
    """
    session = requests.Session()

    # Set browser identity headers for Royal Road requests.
    session.headers.update({"User-Agent": UA, "Accept-Language": "en"})

    return session


def fetch(
    session: requests.Session,
    url: str,
    *,
    mode: FetchMode = FetchMode.REQUIRED,
) -> requests.Response | None:
    """Fetch an HTTP resource with retry handling and optional 404 tolerance.

    Args:
        session: Active requests HTTP session.
        url: The HTTP/HTTPS endpoint URL to query.
        mode: FetchMode controlling whether HTTP 404 is treated as None or an error.

    Returns:
        Response object on success, or None if mode is OPTIONAL and status is 404.

    Raises:
        RuntimeError: If all retries fail or an unhandled HTTP error occurs.
    """
    last_error = "unknown error"

    # Retry requests to mitigate transient network connectivity or throttling errors.
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)

            # When optional, 404 indicates an uninitialized deployment rather than failure.
            if mode == FetchMode.OPTIONAL and response.status_code == HTTPStatus.NOT_FOUND:
                return None

            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = str(exc)

    # Exhausted retry count; raise descriptive error.
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fiction_url(feed: dict) -> str:
    """Build the canonical web URL for a Royal Road fiction overview page.

    Args:
        feed: Fiction feed configuration dict containing 'fiction_id' and 'slug'.

    Returns:
        Canonical fiction URL string.
    """
    return f"{BASE}/fiction/{feed['fiction_id']}/{feed['slug']}"


def syndication_url(feed: dict) -> str:
    """Build the Royal Road RSS syndication feed URL for discovering recent chapters.

    Args:
        feed: Fiction feed configuration dict containing 'fiction_id'.

    Returns:
        Syndication feed URL string.
    """
    return f"{BASE}/fiction/syndication/{feed['fiction_id']}"


def parse_date(value: str | None) -> dt.datetime:
    """Parse an RFC-822 / RFC-2822 pubDate string into a timezone-aware UTC datetime.

    Args:
        value: RFC-822 date string (e.g. 'Tue, 04 Aug 2026 21:00:16 GMT').

    Returns:
        UTC datetime object, defaulting to current UTC time if input is None or malformed.
    """
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except (TypeError, ValueError):
            pass

    return dt.datetime.now(UTC)


def parse_syndication(document: bytes, feed: dict) -> list[dict]:
    """Parse Royal Road's syndication RSS XML to extract recent chapter metadata.

    Args:
        document: Raw bytes of the syndication XML feed.
        feed: Fiction configuration dict containing 'title'.

    Returns:
        List of chapter dictionaries containing 'title', 'link', 'guid', and 'date'.
    """
    # Strip any leading UTF-8 byte order mark before parsing XML.
    root = ET.fromstring(document.lstrip(b"\xef\xbb\xbf"))
    chapters = []
    title_prefix = feed["title"] + " - "

    # Iterate over items up to limit and strip the redundant fiction title prefix.
    for item in root.findall("./channel/item")[:ITEM_LIMIT]:
        link = (item.findtext("link") or "").strip()
        if not link:
            continue

        title = (item.findtext("title") or "Untitled").strip()
        if title.startswith(title_prefix):
            title = title[len(title_prefix):]

        chapters.append(
            {
                "title": title,
                "link": link,
                "guid": (item.findtext("guid") or link).strip(),
                "date": parse_date(item.findtext("pubDate")),
            }
        )

    return chapters


def safe_url(value: str, base_url: str) -> str | None:
    """Resolve a relative URL against a base URL and enforce allowed URI schemes.

    Args:
        value: Target URL string from an href or src attribute.
        base_url: Canonical chapter URL for relative path resolution.

    Returns:
        Absolute URL string if scheme is http, https, or mailto; None otherwise.
    """
    absolute = urljoin(base_url, value.strip())

    # Only permit safe web and email protocols.
    if urlparse(absolute).scheme.lower() in {"http", "https", "mailto"}:
        return absolute

    return None


def extract_chapter(page: str, chapter_url: str) -> str:
    """Extract and sanitize chapter HTML from a Royal Road chapter web page.

    Sanitization steps:
      1. Isolate the main content container: div.chapter-inner.
      2. Identify dynamic CSS watermark classes: rules in <style> with display:none.
      3. Decompose anti-scraping watermarks and author note containers.
      4. Remove scripts, styles, forms, broken images, and next/prev nav links.
      5. Strip unsafe event attributes (onclick, etc.) and rewrite URLs to absolute.

    Args:
        page: Raw HTML string of the fetched chapter page.
        chapter_url: Chapter permalink URL used for relative link resolution.

    Returns:
        Cleaned, sanitized HTML string of the chapter body.

    Raises:
        RuntimeError: If .chapter-inner is missing or the extracted body is empty.
    """
    soup = BeautifulSoup(page, "html.parser")

    # Locate main chapter content element.
    chapter_inner = soup.select_one("div.chapter-inner")
    if chapter_inner is None:
        raise RuntimeError(f"chapter content not found at {chapter_url}")

    # Royal Road inserts anti-scraping watermarks as ordinary text elements whose
    # dynamically generated class is hidden via a page-local CSS rule:
    #   .watermark_class { display: none; }
    # We inspect all <style> blocks to discover and remove these elements.
    hidden_classes: set[str] = set()
    for style in soup.find_all("style"):
        css = style.get_text(" ", strip=False)
        hidden_classes.update(
            re.findall(r"\.([A-Za-z_][\w-]*)\s*\{[^}]*display\s*:\s*none\b", css, re.I | re.S)
        )

    # Isolate chapter container inside clean wrapper div.
    wrapper = BeautifulSoup("<div></div>", "html.parser").div
    assert wrapper is not None
    wrapper.append(chapter_inner.extract())

    # Remove author notes, watermarks, scripts, broken images, and navigation links.
    for note in wrapper.select("div.author-note-portlet, div.author-note"):
        note.decompose()
    for class_name in hidden_classes:
        for watermark in wrapper.select(f".{class_name}"):
            watermark.decompose()
    for tag in wrapper.find_all(DROP_TAGS):
        tag.decompose()
    for image in wrapper.find_all("img"):
        if not image.get("src"):
            image.decompose()
    for link in wrapper.find_all("a"):
        label = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).lower()
        href = str(link.get("href") or "").lower()
        nav_labels = {"previous chapter", "next chapter", "previous", "next"}
        if "www.royalroadl.com" in href or label in nav_labels:
            link.decompose()
            continue
    for text_node in list(wrapper.find_all(string=True)):
        if text_node.strip() == "<-->":
            text_node.extract()

    # Sanitize element attributes and rewrite relative URLs.
    for tag in wrapper.find_all(True):
        for attr in list(tag.attrs):
            name = attr.lower()
            if name.startswith("on") or name not in ALLOWED_ATTRS:
                del tag.attrs[attr]
                continue
            value = tag.attrs.get(attr)
            if name in URL_ATTRS and isinstance(value, str):
                cleaned = safe_url(value, chapter_url)
                if cleaned is None:
                    del tag.attrs[attr]
                else:
                    tag.attrs[attr] = cleaned

    # Serialize cleaned HTML and ensure non-empty text remains.
    body = wrapper.decode_contents(formatter="html").strip()
    if not re.sub(r"<[^>]+>", "", body).strip():
        raise RuntimeError(f"empty chapter content at {chapter_url}")

    return body


def plain_summary(body: str, limit: int = 500) -> str:
    """Generate plain-text excerpt from HTML chapter body for RSS <description>.

    Args:
        body: Chapter HTML content string.
        limit: Maximum character length of summary before truncation.

    Returns:
        Plain-text excerpt truncated cleanly at word boundary with ellipsis.
    """
    # Convert HTML to normalized plain text.
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)

    # Truncate at word boundary if text exceeds limit.
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"

    return text


def render_item(chapter: dict) -> str:
    """Render chapter metadata and body into an RSS 2.0 <item> XML string.

    Args:
        chapter: Chapter dictionary containing 'title', 'link', 'guid', 'date', and 'body_html'.

    Returns:
        Complete <item>...</item> XML block.
    """
    # Escape CDATA terminator sequence in HTML body.
    body = chapter["body_html"]
    cdata_body = body.replace("]]>", "]]]]><![CDATA[>")

    # Assemble RSS item block.
    return (
        "    <item>\n"
        f"      <title>{escape(chapter['title'])}</title>\n"
        f"      <link>{escape(chapter['link'])}</link>\n"
        f"      <guid isPermaLink=\"false\">{escape(chapter['guid'])}</guid>\n"
        f"      <pubDate>{format_datetime(chapter['date'])}</pubDate>\n"
        f"      <description>{escape(plain_summary(body))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata_body}]]></content:encoded>\n"
        "    </item>"
    )


def load_existing(session: requests.Session, feed: dict) -> dict[str, dict]:
    """Load previously rendered chapters from local file and optional remote deployment.

    Args:
        session: Active requests HTTP session.
        feed: Fiction feed configuration dict containing 'key'.

    Returns:
        Dictionary mapping chapter link URL to chapter dictionary.
    """
    documents: list[bytes] = []

    # Load local feed file if available.
    local_path = OUT_DIR / feed["key"] / "feed.xml"
    if local_path.is_file():
        documents.append(local_path.read_bytes())

    # Fetch live remote feed XML to retain chapters across deployment runs.
    if SITE_BASE_URL:
        feed_url = f"{SITE_BASE_URL}/{feed['key']}/feed.xml"
        response = fetch(session, feed_url, mode=FetchMode.OPTIONAL)
        if response is not None:
            documents.append(response.content)

    # Parse and index existing chapters by link URL.
    chapters: dict[str, dict] = {}
    for document in documents:
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            continue

        for item in root.findall("./channel/item"):
            link = (item.findtext("link") or "").strip()
            body = item.findtext(f"{{{CONTENT_NS}}}encoded") or ""
            date = parse_date(item.findtext("pubDate"))
            if link and body:
                chapters[link] = {
                    "title": (item.findtext("title") or "Untitled").strip(),
                    "link": link,
                    "guid": (item.findtext("guid") or link).strip(),
                    "date": date,
                    "body_html": body,
                }

    return chapters


def merge_new_chapters(
    existing: dict[str, dict],
    listing: list[dict],
    body_loader,
) -> tuple[list[dict], int, int]:
    """Merge newly published chapters, fetch bodies, and evict oldest beyond limit.

    Args:
        existing: Mapping of chapter link -> chapter dict from cache.
        listing: Freshly fetched chapter metadata list from syndication feed.
        body_loader: Callable taking chapter dict and returning full chapter HTML.

    Returns:
        Tuple of (ordered_chapters_list, newly_fetched_count, evicted_count).
    """
    merged = dict(existing)
    known_guids = {chapter["guid"] for chapter in existing.values()}
    newest_existing = max((chapter["date"] for chapter in existing.values()), default=None)
    fetched = 0

    # Fetch and append unseen chapters strictly newer than latest retained item.
    for chapter in listing:
        if chapter["link"] in merged or chapter["guid"] in known_guids:
            continue
        if newest_existing is not None and chapter["date"] <= newest_existing:
            continue

        new_chapter = dict(chapter)
        new_chapter["body_html"] = body_loader(new_chapter)
        merged[new_chapter["link"]] = new_chapter
        known_guids.add(new_chapter["guid"])
        fetched += 1

    # Order newest first and evict oldest items beyond cap.
    ordered = sorted(
        merged.values(),
        key=lambda chapter: (chapter["date"], chapter["guid"]),
        reverse=True,
    )
    evicted = max(0, len(ordered) - ITEM_LIMIT)

    return ordered[:ITEM_LIMIT], fetched, evicted


# Format feed title as 'novel title - Author'.
def feed_title(feed: dict) -> str:
    title = feed.get("title")
    author = feed.get("author")
    if title and author:
        return f"{title} - {author}"

    return title or author or ""


def build_feed_xml(feed: dict, chapters: list[dict]) -> str:
    """Assemble complete RSS 2.0 XML string for fiction channel.

    Args:
        feed: Fiction feed configuration dict.
        chapters: List of chapter dicts to include in channel.

    Returns:
        Complete UTF-8 encoded RSS 2.0 XML document string.
    """
    # Build channel metadata and optional self atom:link.
    now = format_datetime(dt.datetime.now(UTC))
    self_link = (
        f'    <atom:link href="{escape(SITE_BASE_URL + "/" + feed["key"] + "/feed.xml")}" '
        'rel="self" type="application/rss+xml" />\n'
        if SITE_BASE_URL
        else ""
    )
    title = feed_title(feed)
    description = f"Unofficial full-text feed of {feed['title']} by {feed['author']} on Royal Road."

    # Assemble complete RSS document.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(fiction_url(feed))}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        f"{self_link}"
        + "\n".join(render_item(chapter) for chapter in chapters)
        + "\n  </channel>\n</rss>\n"
    )


def write_feed(feed: dict, xml: str, count: int) -> None:
    """Write feed XML and companion HTML index page to output directory.

    Args:
        feed: Fiction feed configuration dict.
        xml: Generated RSS XML document string.
        count: Total number of chapters contained in the feed.
    """
    # Create target directory.
    directory = OUT_DIR / feed["key"]
    directory.mkdir(parents=True, exist_ok=True)

    # Write RSS XML file.
    (directory / "feed.xml").write_text(xml, encoding="utf-8")

    # Write HTML index page.
    title = feed_title(feed)
    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — Royal Road RSS</title>"
        f"<h1>{html.escape(title)} — Royal Road RSS</h1>"
        "<p>Unofficial full-text feed generated from Royal Road.</p>"
        "<p><a href='feed.xml'>Subscribe to feed.xml</a></p>"
        f"<p>{count} items.</p>"
    )
    (directory / "index.html").write_text(page, encoding="utf-8")


def is_royalroad_feed_dir(path: Path) -> bool:
    """Verify whether a directory contains files generated by the Royal Road feed builder.

    Args:
        path: Path to directory under OUT_DIR.

    Returns:
        True if the directory contains Royal Road signatures in feed.xml or index.html.
    """
    if not path.is_dir():
        return False

    # Check feed.xml content for Royal Road domain or title signature.
    feed_file = path / "feed.xml"
    if feed_file.is_file():
        try:
            content = feed_file.read_text(encoding="utf-8", errors="replace")
            if "pawchive.pw" in content:
                return False
            if "royalroad.com" in content or "Royal Road" in content:
                return True
        except OSError:
            pass

    # Check index.html content as fallback.
    index_file = path / "index.html"
    if index_file.is_file():
        try:
            content = index_file.read_text(encoding="utf-8", errors="replace")
            if "Pawchive" in content:
                return False
            if "Royal Road" in content:
                return True
        except OSError:
            pass

    return False


def prune_untracked_feeds(out_dir: Path, active_keys: set[str]) -> list[str]:
    """Remove feed directories in out_dir that belong to Royal Road but are no longer tracked.

    Args:
        out_dir: Path to root output directory.
        active_keys: Set of active feed key strings from FEEDS.

    Returns:
        Sorted list of pruned directory names.
    """
    if not out_dir.is_dir():
        return []

    # Find and delete untracked Royal Road directories.
    removed: list[str] = []
    for child in out_dir.iterdir():
        if child.is_dir() and child.name not in active_keys and is_royalroad_feed_dir(child):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as err:
                print(f"Failed to remove untracked feed directory {child}: {err}")

    return sorted(removed)


def run_feed(session: requests.Session, feed: dict) -> int:
    """Execute complete feed build pipeline for a Royal Road fiction.

    Args:
        session: Active requests HTTP session.
        feed: Fiction feed configuration dict.

    Returns:
        Number of items retained in the final feed.
    """
    print(f"[{feed['key']}] {feed_title(feed)}")

    # Load existing items and fetch latest syndication feed.
    existing = load_existing(session, feed)
    listing = fetch(session, syndication_url(feed))
    assert listing is not None
    chapters = parse_syndication(listing.content, feed)
    if not chapters:
        raise RuntimeError(f"no chapters found for {feed['title']}")

    detail_fetches = 0

    # Rate-limited callback to fetch missing chapter bodies.
    def load_body(chapter: dict) -> str:
        nonlocal detail_fetches
        if detail_fetches:
            time.sleep(REQUEST_DELAY)
        response = fetch(session, chapter["link"])
        assert response is not None
        detail_fetches += 1
        return extract_chapter(response.text, chapter["link"])

    # Merge chapters and write outputs.
    merged, fetched, evicted = merge_new_chapters(existing, chapters, load_body)
    write_feed(feed, build_feed_xml(feed, merged), len(merged))

    print(
        f"  listing items: {len(chapters)}; retained: {len(existing)}; "
        f"newly fetched/rendered: {fetched}; evicted oldest: {evicted}; feed items: {len(merged)}"
    )

    return len(merged)


def main() -> int:
    """Run feed generation and pruning for all configured Royal Road feeds.

    Returns:
        Exit code (0 for success).
    """
    session = make_session()

    # Prune untracked Royal Road feed directories.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pruned = prune_untracked_feeds(OUT_DIR, {feed["key"] for feed in FEEDS})
    if pruned:
        print(f"Pruned untracked Royal Road feed(s): {', '.join(pruned)}")

    # Build all active feeds.
    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
