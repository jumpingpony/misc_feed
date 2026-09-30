#!/usr/bin/env python3
"""Build full-text RSS feeds for selected Royal Road fiction.

Royal Road's syndication feeds provide the latest chapter metadata, but their
bodies end in ``(...)``.  This builder uses those feeds for discovery, fetches
only missing chapter pages, and extracts the complete ``div.chapter-inner`` HTML.

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

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.  Shared
plumbing lives in rss_common.py; this builder keeps source-specific logic.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import requests

import rss_common as rss


BASE = "https://www.royalroad.com"
CONTENT_NS = rss.CONTENT_NS

FEEDS: list[dict[str, str]] = []

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


def make_session() -> requests.Session:
    """Create a configured requests.Session for Royal Road HTTP interactions.

    Returns:
        A requests.Session pre-configured with a browser User-Agent and English
        Accept-Language headers to ensure consistent HTML responses.
    """
    return rss.make_session()


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
    return rss.parse_rfc822_datetime(value) or dt.datetime.now(rss.UTC)


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
            if name in rss.URL_ATTRS and isinstance(value, str):
                cleaned = rss.safe_url(value, chapter_url)
                if cleaned is None:
                    del tag.attrs[attr]
                else:
                    tag.attrs[attr] = cleaned

    # Serialize cleaned HTML and ensure non-empty text remains.
    body = wrapper.decode_contents(formatter="html").strip()
    if not re.sub(r"<[^>]+>", "", body).strip():
        raise RuntimeError(f"empty chapter content at {chapter_url}")

    return body


def render_item(chapter: dict) -> str:
    """Render chapter metadata and body into an RSS 2.0 <item> XML string.

    Args:
        chapter: Chapter dictionary containing 'title', 'link', 'guid', 'date', and 'body_html'.

    Returns:
        Complete <item>...</item> XML block.
    """
    return rss.render_rss_item(
        title=chapter["title"],
        link=chapter["link"],
        guid=chapter["guid"],
        published=chapter["date"],
        body=chapter["body_html"],
        guid_kind=rss.GuidKind.IDENTIFIER,
    )


def load_existing(session: requests.Session, feed: dict) -> dict[str, dict]:
    """Load previously rendered chapters from local file and optional remote deployment.

    Args:
        session: Active requests HTTP session.
        feed: Fiction feed configuration dict containing 'key'.

    Returns:
        Dictionary mapping chapter link URL to chapter dictionary.
    """
    documents = rss.load_feed_documents(
        session,
        out_dir=OUT_DIR,
        key=feed["key"],
        site_base_url=SITE_BASE_URL,
        timeout=TIMEOUT,
        retries=RETRIES,
    )

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
    merged, fetched, evicted = rss.merge_new_items(
        {link: (chapter["date"], chapter) for link, chapter in existing.items()},
        listing,
        identity=lambda chapter: chapter["link"],
        published=lambda chapter: chapter["date"],
        load=lambda chapter: {**chapter, "body_html": body_loader(chapter)},
        limit=ITEM_LIMIT,
        guid=lambda chapter: chapter.get("guid"),
        existing_guids={chapter["guid"] for chapter in existing.values()},
    )

    return [chapter for _, chapter in merged.values()], fetched, evicted


# Format feed title as 'novel title - Author'.
def feed_title(feed: dict) -> str:
    return rss.feed_title(feed)


def build_feed_xml(feed: dict, chapters: list[dict]) -> str:
    """Assemble complete RSS 2.0 XML string for fiction channel.

    Args:
        feed: Fiction feed configuration dict.
        chapters: List of chapter dicts to include in channel.

    Returns:
        Complete UTF-8 encoded RSS 2.0 XML document string.
    """
    # Build channel metadata and optional self atom:link.
    title = feed_title(feed)
    description = f"Unofficial full-text feed of {feed['title']} by {feed['author']} on Royal Road."
    self_url = f"{SITE_BASE_URL}/{feed['key']}/feed.xml" if SITE_BASE_URL else None

    return rss.build_feed_document(
        title=title,
        link=fiction_url(feed),
        description=description,
        item_xml=(render_item(chapter) for chapter in chapters),
        self_url=self_url,
    )


def write_feed(feed: dict, xml: str, count: int) -> None:
    """Write feed XML and companion HTML index page to output directory.

    Args:
        feed: Fiction feed configuration dict.
        xml: Generated RSS XML document string.
        count: Total number of chapters contained in the feed.
    """
    rss.write_feed_files(
        OUT_DIR,
        feed["key"],
        xml,
        title=feed_title(feed),
        page_suffix="Royal Road RSS",
        description="Unofficial full-text feed generated from Royal Road.",
        count=count,
    )


def is_royalroad_feed_dir(path: Path) -> bool:
    """Verify whether a directory contains files generated by the Royal Road feed builder.

    Args:
        path: Path to directory under OUT_DIR.

    Returns:
        True if the directory contains Royal Road signatures in feed.xml or index.html.
    """
    return rss.directory_has_signature(
        path,
        feed_markers=("royalroad.com", "Royal Road"),
        page_markers=("Royal Road",),
        exclude=("pawchive.pw", "Pawchive"),
    )


def prune_untracked_feeds(out_dir: Path, active_keys: set[str]) -> list[str]:
    """Remove feed directories in out_dir that belong to Royal Road but are no longer tracked.

    Args:
        out_dir: Path to root output directory.
        active_keys: Set of active feed key strings from FEEDS.

    Returns:
        Sorted list of pruned directory names.
    """
    return rss.prune_untracked_feeds(out_dir, active_keys, is_royalroad_feed_dir)


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
    listing = rss.fetch(
        session, syndication_url(feed), timeout=TIMEOUT, retries=RETRIES
    )
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
        response = rss.fetch(session, chapter["link"], timeout=TIMEOUT, retries=RETRIES)
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
