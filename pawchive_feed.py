#!/usr/bin/env python3
"""Build full-text RSS feeds for selected Pawchive Patreon creators.

Post discovery and post bodies come from Pawchive's documented v1 API.  Chapter
HTML is read from the same API field used by WebToEpub's PawchiveParser. Explicit
previous/next navigation and attachments are omitted; author notes remain.

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.
"""
from __future__ import annotations

import argparse
import datetime as dt
from email.utils import format_datetime, parsedate_to_datetime
from enum import Enum, auto
import html
from html.parser import HTMLParser
from http import HTTPStatus
import json
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests


BASE = "https://pawchive.pw"
API_BASE = BASE + "/api/v1"
PATREON_API_BASE = "https://www.patreon.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.7922.76 Safari/537.36"
)
UTC = dt.timezone.utc

DEFAULT_SESSION_COOKIE = (
    "eyJfcGVybWFuZW50Ijp0cnVlLCJhY2NvdW50X2lkIjozNDU4MjJ9."
    "aocWXA.DPjQUj10mUuEqXX3zexPW9HZvlI"
)
SESSION_COOKIE = (os.environ.get("PAWCHIVE_SESSION") or DEFAULT_SESSION_COOKIE).strip()

FEEDS = [
    {
        "key": "cerim",
        "creator_id": "31891971",
        "campaign_id": "10143762",
        "fallback_name": "cerim",
    },
]

ITEM_LIMIT = max(1, int(os.environ.get("PAWCHIVE_MAX_ITEMS", "10")))
TIMEOUT = max(1, int(os.environ.get("PAWCHIVE_TIMEOUT", "30")))
RETRIES = max(0, int(os.environ.get("PAWCHIVE_RETRIES", "2")))
OUT_DIR = Path(os.environ.get("PAWCHIVE_OUT_DIR", "public"))
SITE_BASE_URL = os.environ.get("PAWCHIVE_SITE_BASE_URL", "").strip().rstrip("/")

DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template"}
VOID_TAGS = {
    "area", "base", "br", "col", "hr", "img", "input", "link", "meta",
    "source", "track", "wbr",
}
KNOWN_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo", "blockquote", "br",
    "caption", "cite", "code", "col", "colgroup", "data", "dd", "del", "details", "dfn",
    "div", "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav",
    "ol", "p", "picture", "pre", "q", "s", "samp", "section", "small", "source", "span",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "time", "tr", "u", "ul", "var", "wbr",
}
ALLOWED_ATTRS = {
    "alt", "class", "colspan", "datetime", "height", "href", "lang", "loading", "rel",
    "rowspan", "src", "srcset", "title", "width",
}
URL_ATTRS = {"href", "src"}

NAVIGATION_BLOCK_RE = re.compile(
    r"(?:"
    r"<blockquote\b[^>]*>\s*<p\b[^>]*>\s*"
    r"|<p\b[^>]*>\s*"
    r")"
    r"<a\b[^>]*>\s*(?:previous|next)\s+chapter\s*</a>\s*"
    r"</p>\s*(?:</blockquote>)?",
    re.I,
)


# Patreon synchronization behavior modes.
class SyncMode(Enum):
    SYNC_PATREON = auto()
    SKIP_PATREON = auto()


# Create HTTP session configured with headers and Pawchive auth cookie.
def make_session(cookie: str | None = None) -> requests.Session:
    session = requests.Session()

    # Set default browser headers for API requests.
    session.headers.update({"User-Agent": UA, "Accept-Language": "en", "Accept": "application/json"})

    # Attach session cookie when available for authenticated endpoints.
    active_cookie = cookie if cookie is not None else SESSION_COOKIE
    if active_cookie:
        session.cookies.set("session", active_cookie, domain="pawchive.pw")

    return session


# Fetch and decode JSON from URL with retry handling.
def fetch_json(session: requests.Session, url: str) -> object:
    last_error = "unknown error"

    # Retry transient network and JSON decode failures.
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = str(exc)

    raise RuntimeError(f"failed to fetch {url}: {last_error}")


# Fetch published feed XML; return None on 404 for initial deployment.
def fetch_existing(session: requests.Session, url: str) -> bytes | None:
    last_error = "unknown error"

    # Retry remote request to tolerate transient outages.
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            if response.status_code == HTTPStatus.NOT_FOUND:
                return None
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last_error = str(exc)

    raise RuntimeError(f"failed to load published feed {url}: {last_error}")


# Resolve relative URL and enforce allowed URI schemes.
def safe_url(value: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, value.strip())

    # Allow only web and mail protocols; block script execution vectors.
    if urlparse(absolute).scheme.lower() in {"http", "https", "mailto"}:
        return absolute

    return None


class ChapterSanitizer(HTMLParser):
    """HTML parser that strips dangerous tags, sanitizes attributes, and escapes unknown tags.

    This parser enforces an allowlist of safe HTML elements and attributes. Elements that
    contain active scripting or embedding (such as <script>, <style>, <iframe>) are discarded
    entirely along with their children. Unknown tags are escaped to visible text entities.
    """

    def __init__(self, base_url: str):
        """Initialize the sanitizer parser state.

        Args:
            base_url: Base URL of the post, used to resolve relative href and src attributes.
        """
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.parts: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Process an opening HTML tag against drop rules and allowlists.

        Args:
            tag: Name of the HTML tag (e.g., 'p', 'a', 'script').
            attrs: List of (name, value) attribute pairs.
        """
        tag = tag.lower()

        # If the tag is in DROP_WITH_CONTENT (e.g., <script>), increment drop depth
        # to ensure all nested text and child elements are discarded until closed.
        if tag in DROP_WITH_CONTENT:
            self.drop_depth += 1
            return

        # Skip emitting any markup while inside a dropped element subtree.
        if self.drop_depth:
            return

        # Escape unrecognized tags to display them as literal text instead of HTML elements.
        if tag not in KNOWN_TAGS:
            self.parts.append(html.escape(self.get_starttag_text() or f"<{tag}>"))
            return

        # Filter element attributes against the ALLOWED_ATTRS allowlist.
        # Clean and rewrite URL attributes (href, src) to absolute safe URLs.
        clean_attrs = []
        for name, value in attrs:
            name = name.lower()
            if name not in ALLOWED_ATTRS or value is None:
                continue
            if name in URL_ATTRS:
                value = safe_url(value, self.base_url)
                if value is None:
                    continue
            clean_attrs.append(f' {name}="{html.escape(value, quote=True)}"')

        self.parts.append(f"<{tag}{''.join(clean_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Process a self-closing HTML tag (e.g., <img src="..." />).
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        # Process a closing HTML tag and decrement drop depth when closing a discarded tag.
        tag = tag.lower()

        # Decrement drop depth when exiting a container whose children were discarded.
        if tag in DROP_WITH_CONTENT:
            if self.drop_depth:
                self.drop_depth -= 1
            return

        # Skip closing tags when still inside a dropped container.
        if self.drop_depth:
            return

        # Escape unknown closing tags; omit closing tag for void elements (e.g. <br>, <img>).
        if tag not in KNOWN_TAGS:
            self.parts.append(html.escape(f"</{tag}>"))
        elif tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        # Append text content to output buffer after escaping special HTML characters.
        if not self.drop_depth:
            self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        # Preserve named HTML character references (e.g., &amp;, &lt;).
        if not self.drop_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        # Preserve numeric HTML character references (e.g., &#123;, &#x7B;).
        if not self.drop_depth:
            self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        # Return the fully sanitized HTML document as a string.
        return "".join(self.parts)


def remove_chapter_fluff(source_html: str) -> str:
    """Remove previous and next chapter navigation links while preserving story text.

    Pawchive posts often include navigation blocks such as:
        <blockquote><p><a href="...">Previous Chapter</a></p></blockquote>
    These links are redundant in RSS readers and are stripped by regex matching.

    Args:
        source_html: Raw HTML content string from the post.

    Returns:
        HTML string with navigation blocks removed.
    """
    return NAVIGATION_BLOCK_RE.sub("", source_html).strip()


def build_chapter(post: dict) -> str:
    """Extract, sanitize, and prepare the complete chapter HTML body for RSS embedding.

    Args:
        post: Post dictionary retrieved from Pawchive API.

    Returns:
        Sanitized HTML string safe for inclusion within an XML CDATA block.
    """
    post_url = post_permalink(post)

    # Run the raw HTML content through navigation cleanup and tag allowlist filtering.
    sanitizer = ChapterSanitizer(post_url)
    sanitizer.feed(remove_chapter_fluff(str(post.get("content") or "")))
    sanitizer.close()

    # XML CDATA blocks cannot contain the sequence ']]>'.
    # Splitting into ']]]]><![CDATA[>' preserves the literal character sequence in XML parsers.
    body = sanitizer.get_html()
    return body.replace("]]>", "]]]]><![CDATA[>")


def parse_date(value: object) -> dt.datetime:
    """Parse an ISO-8601 formatted timestamp string into a timezone-aware UTC datetime.

    Args:
        value: Timestamp string (e.g., '2026-08-20T10:00:00Z' or '2026-08-20T10:00:00+00:00').

    Returns:
        Datetime instance normalized to UTC. Defaults to dt.datetime.now(UTC) if parsing fails.
    """
    # Attempt ISO-8601 string parsing when valid input is provided.
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            pass

    # Fallback to current time if the date field was missing or invalid.
    return dt.datetime.now(UTC)


def post_permalink(post: dict) -> str:
    """Build the canonical Pawchive web URL for a given post.

    Example:
        >>> post = {"service": "patreon", "user": "31891971", "id": "167183185"}
        >>> post_permalink(post)
        'https://pawchive.pw/patreon/user/31891971/post/167183185'

    Args:
        post: Post dictionary containing 'service', 'user', and 'id'.

    Returns:
        Canonical permalink URL string.
    """
    service = post.get("service", "patreon")
    user = post.get("user", "")
    post_id = post.get("id", "")
    return f"{BASE}/{service}/user/{user}/post/{post_id}"


def plain_summary(body: str, limit: int = 500) -> str:
    """Create a plain-text excerpt from chapter HTML for the RSS <description> tag.

    Args:
        body: Chapter HTML content string.
        limit: Maximum character length of the summary before truncation.

    Returns:
        Plain-text summary truncated cleanly at the last space boundary with an ellipsis.
    """
    # Strip HTML tags and collapse whitespace runs into single spaces.
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text).strip()

    # Truncate text at word boundary if it exceeds the length limit.
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"

    return text


def render_item(post: dict) -> str:
    """Render a Pawchive post dictionary into an RSS 2.0 <item> XML string.

    Args:
        post: Post dictionary from the Pawchive API.

    Returns:
        Complete <item>...</item> XML block with title, link, pubDate, description,
        and full content:encoded CDATA payload.
    """
    # Extract metadata fields with fallback defaults.
    title = str(post.get("title") or "Untitled").strip()
    link = post_permalink(post)
    published = parse_date(post.get("published") or post.get("added"))
    body = build_chapter(post)

    # Format into standard RSS 2.0 item XML structure.
    return (
        "    <item>\n"
        f"      <title>{escape(title)}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"true\">{escape(link)}</guid>\n"
        f"      <pubDate>{format_datetime(published)}</pubDate>\n"
        f"      <description>{escape(plain_summary(body))}</description>\n"
        f"      <content:encoded><![CDATA[{body}]]></content:encoded>\n"
        "    </item>"
    )


ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_RE = re.compile(r"<guid[^>]*>(.*?)</guid>", re.S)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


def parse_rss_date(value: str) -> dt.datetime | None:
    """Parse an RFC-822 / RFC-2822 date string from an existing RSS feed document.

    Args:
        value: Date string (e.g., 'Tue, 04 Aug 2026 21:00:16 +0000').

    Returns:
        Timezone-aware UTC datetime, or None if parsing fails.
    """
    try:
        parsed = parsedate_to_datetime(html.unescape(value).strip())
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def parse_existing_items(document: str) -> dict[str, tuple[dt.datetime, str]]:
    """Extract existing rendered <item> XML blocks from an RSS document without re-parsing HTML.

    Args:
        document: Full XML document string of a previously generated feed.

    Returns:
        Dictionary mapping item identity (guid or link URL) to a tuple of
        (published_datetime, raw_item_xml_string).
    """
    items: dict[str, tuple[dt.datetime, str]] = {}

    # Scan the document for <item> blocks using regular expressions to avoid re-serializing bodies.
    for match in ITEM_RE.finditer(document):
        block = match.group(0).strip()
        guid_match = GUID_RE.search(block)
        link_match = LINK_RE.search(block)
        date_match = PUBDATE_RE.search(block)
        identity_match = guid_match or link_match

        # Skip incomplete item blocks lacking identifier or publication date.
        if identity_match is None or date_match is None:
            continue

        published = parse_rss_date(date_match.group(1))
        if published is None:
            continue

        identity = html.unescape(identity_match.group(1)).strip()
        if identity:
            items[identity] = (published, block)

    return items


def load_existing(session: requests.Session, feed: dict) -> dict[str, tuple[dt.datetime, str]]:
    """Load previously rendered items from local disk and optional production deployment.

    Args:
        session: Active requests HTTP session.
        feed: Feed configuration dictionary containing 'key'.

    Returns:
        Dictionary mapping item identity to (published_datetime, raw_item_xml).
    """
    documents: list[str] = []

    # Read locally cached feed XML file if it exists on disk.
    local_path = OUT_DIR / feed["key"] / "feed.xml"
    if local_path.is_file():
        documents.append(local_path.read_text(encoding="utf-8"))

    # Fetch live feed XML from site base URL to retain historical items on fresh CI checkouts.
    if SITE_BASE_URL:
        remote = fetch_existing(session, f"{SITE_BASE_URL}/{feed['key']}/feed.xml")
        if remote is not None:
            documents.append(remote.decode("utf-8-sig"))

    # Merge items across all loaded documents (local and remote).
    items: dict[str, tuple[dt.datetime, str]] = {}
    for document in documents:
        items.update(parse_existing_items(document))

    return items


def collect_posts(session: requests.Session, creator_id: str) -> list[dict]:
    """Fetch the latest posts for a creator from the Pawchive API.

    Pawchive enforces fixed 50-result API pages. We query page 0 (newest posts)
    and retain up to ITEM_LIMIT sorted entries.

    Args:
        session: Active requests HTTP session.
        creator_id: Pawchive creator user ID.

    Returns:
        List of post dictionaries sorted newest first up to ITEM_LIMIT.
    """
    # Fetch first page of 50 posts from Pawchive API.
    url = f"{API_BASE}/patreon/user/{creator_id}?o=0"
    payload = fetch_json(session, url)
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected creator-post response from {url}")

    # Deduplicate posts by permalink to guard against duplicate entries in API payload.
    posts_by_link = {
        post_permalink(post): post
        for post in payload
        if isinstance(post, dict) and post.get("id")
    }
    posts = list(posts_by_link.values())

    # Sort posts descending by publication date and retain top slice.
    posts.sort(
        key=lambda post: parse_date(post.get("published") or post.get("added")),
        reverse=True,
    )
    return posts[:ITEM_LIMIT]


def merge_new_posts(
    existing: dict[str, tuple[dt.datetime, str]],
    posts: list[dict],
    renderer=render_item,
) -> tuple[dict[str, tuple[dt.datetime, str]], int, int]:
    """Merge newly discovered posts into existing feed items with deduplication and eviction.

    Flow:
        1. Identify newest publication timestamp in existing items.
        2. Filter incoming posts: only render items newer than newest existing date.
        3. Sort merged items descending by publication date.
        4. Evict oldest items that exceed ITEM_LIMIT.

    Args:
        existing: Mapping of identity -> (published_date, item_xml).
        posts: List of freshly fetched post dictionaries.
        renderer: Callable converting a post dict to an item XML string.

    Returns:
        Tuple of (merged_items_dict, newly_rendered_count, evicted_count).
    """
    merged = dict(existing)
    newest_existing = max((value[0] for value in existing.values()), default=None)
    rendered = 0

    # Add only unseen posts that are newer than latest retained entry.
    for post in posts:
        identity = post_permalink(post)
        published = parse_date(post.get("published") or post.get("added"))

        # Skip already retained posts or posts older than our newest retained item.
        if identity in merged:
            continue
        if newest_existing is not None and published <= newest_existing:
            continue

        # Render HTML body and XML block for new post.
        merged[identity] = (published, renderer(post).strip())
        rendered += 1

    # Sort merged collection by date descending and evict excess oldest items.
    ordered = sorted(merged.items(), key=lambda value: (value[1][0], value[0]), reverse=True)
    evicted = max(0, len(ordered) - ITEM_LIMIT)

    return dict(ordered[:ITEM_LIMIT]), rendered, evicted


def build_feed_xml(feed: dict, name: str, items: dict[str, tuple[dt.datetime, str]]) -> str:
    """Construct a full RSS 2.0 XML feed document.

    Args:
        feed: Feed configuration dictionary.
        name: Display name of the creator.
        items: Mapping of identity -> (published_date, item_xml).

    Returns:
        Complete UTF-8 encoded RSS 2.0 XML string.
    """
    # Sort items chronologically descending.
    ordered = sorted(items.values(), key=lambda value: value[0], reverse=True)

    # Build channel metadata and optional self-referencing atom:link header.
    creator_url = f"{BASE}/patreon/user/{feed['creator_id']}"
    title = f"{name} — Pawchive"
    description = f"Unofficial full-text feed of {name}'s Patreon posts archived by Pawchive."
    self_link = (
        f'    <atom:link href="{escape(SITE_BASE_URL + "/" + feed["key"] + "/feed.xml")}" '
        'rel="self" type="application/rss+xml" />\n'
        if SITE_BASE_URL
        else ""
    )

    # Assemble complete RSS XML document.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(creator_url)}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{format_datetime(dt.datetime.now(UTC))}</lastBuildDate>\n"
        f"{self_link}"
        + "\n".join(item for _, item in ordered)
        + "\n  </channel>\n</rss>\n"
    )


def write_feed(feed: dict, name: str, xml: str, count: int) -> None:
    """Write feed.xml and companion HTML index page to output directory.

    Args:
        feed: Feed configuration dictionary.
        name: Display name of the creator.
        xml: Generated RSS XML document string.
        count: Number of items contained in the feed.
    """
    # Create target feed output directory.
    directory = OUT_DIR / feed["key"]
    directory.mkdir(parents=True, exist_ok=True)

    # Write feed.xml file.
    (directory / "feed.xml").write_text(xml, encoding="utf-8")

    # Generate and write companion index.html landing page.
    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(name)} — Pawchive RSS</title>"
        f"<h1>{html.escape(name)} — Pawchive RSS</h1>"
        "<p>Unofficial full-text feed generated from the Pawchive API.</p>"
        "<p><a href='feed.xml'>Subscribe to feed.xml</a></p>"
        f"<p>{count} items.</p>"
    )
    (directory / "index.html").write_text(page, encoding="utf-8")


def is_pawchive_feed_dir(path: Path) -> bool:
    """Verify whether a directory contains files generated by the Pawchive feed builder.

    Args:
        path: Path to directory in OUT_DIR.

    Returns:
        True if the directory contains a feed.xml or index.html mentioning Pawchive signatures.
    """
    if not path.is_dir():
        return False

    # Check feed.xml content for Pawchive domain or title signature.
    feed_file = path / "feed.xml"
    if feed_file.is_file():
        try:
            content = feed_file.read_text(encoding="utf-8", errors="replace")
            if "pawchive.pw" in content or "Pawchive" in content:
                return True
        except OSError:
            pass

    # Check index.html content as fallback signature.
    index_file = path / "index.html"
    if index_file.is_file():
        try:
            content = index_file.read_text(encoding="utf-8", errors="replace")
            if "Pawchive" in content:
                return True
        except OSError:
            pass

    return False


def prune_untracked_feeds(out_dir: Path, active_keys: set[str]) -> list[str]:
    """Delete output directories belonging to Pawchive feeds that are no longer configured.

    Args:
        out_dir: Output root directory path.
        active_keys: Set of active feed key strings from FEEDS.

    Returns:
        Sorted list of pruned directory names.
    """
    if not out_dir.is_dir():
        return []

    # Iterate through child directories and remove obsolete Pawchive feeds.
    removed: list[str] = []
    for child in out_dir.iterdir():
        if child.is_dir() and child.name not in active_keys and is_pawchive_feed_dir(child):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as err:
                print(f"Failed to remove untracked feed directory {child}: {err}")

    return sorted(removed)


def parse_post_identifier(
    target: str,
    default_creator: str = "31891971",
    default_service: str = "patreon",
) -> tuple[str, str, str]:
    """Extract (service, creator_id, post_id) from raw ID, Patreon URL, or Pawchive URL.

    Supported patterns:
        - Pawchive URL: 'https://pawchive.pw/patreon/user/31891971/post/167183185'
        - Patreon URL:  'https://www.patreon.com/posts/167183185' or slug variants
        - Numeric ID:   '167183185' (uses default service and creator)

    Args:
        target: URL or numeric string input.
        default_creator: Fallback creator ID if not specified in URL.
        default_service: Fallback service name ('patreon').

    Returns:
        Tuple of (service, creator_id, post_id).

    Raises:
        ValueError: If the input cannot be parsed as a post identifier.
    """
    s = target.strip()

    # Match Pawchive URL pattern.
    m_paw = re.search(r"pawchive\.(?:pw|st)/([^/]+)/user/([^/]+)/post/(\d+)", s)
    if m_paw:
        return m_paw.group(1), m_paw.group(2), m_paw.group(3)

    # Match Patreon URL pattern.
    m_pat = re.search(r"patreon\.com/.*?posts/(?:.*-)?(\d+)", s)
    if m_pat:
        return default_service, default_creator, m_pat.group(1)

    # Match standalone numeric post ID.
    m_id = re.fullmatch(r"\d+", s)
    if m_id:
        return default_service, default_creator, m_id.group(0)

    raise ValueError(f"Unable to parse post identifier or URL from: {target!r}")


def check_post_flag(
    session: requests.Session,
    service: str,
    creator_id: str,
    post_id: str,
) -> bool:
    """Check if a post is currently in Pawchive's re-import queue without mutating state.

    Args:
        session: Active requests HTTP session.
        service: Service name (e.g., 'patreon').
        creator_id: Creator ID string.
        post_id: Post ID string.

    Returns:
        True if the post is already flagged (HTTP 200), False if not flagged (HTTP 404).
    """
    url = f"{API_BASE}/{service}/user/{creator_id}/post/{post_id}/flag"

    # Query Pawchive flag status endpoint with retries.
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            if response.status_code == HTTPStatus.OK:
                return True
            if response.status_code == HTTPStatus.NOT_FOUND:
                return False
        except requests.RequestException:
            pass

    return False


def flag_post_politely(
    session: requests.Session,
    service: str,
    creator_id: str,
    post_id: str,
) -> tuple[bool, str]:
    """Request Pawchive to re-import a post, skipping if already flagged.

    Args:
        session: Active authenticated requests HTTP session.
        service: Service name ('patreon').
        creator_id: Creator ID string.
        post_id: Post ID string.

    Returns:
        Tuple of (success_boolean, reason_message).
    """
    # Check current flag status first to avoid redundant POST requests.
    if check_post_flag(session, service, creator_id, post_id):
        return True, "already flagged"

    url = f"{API_BASE}/{service}/user/{creator_id}/post/{post_id}/flag"
    last_error = "unknown error"

    # Submit POST flag request with retries.
    for _ in range(RETRIES + 1):
        try:
            response = session.post(url, timeout=TIMEOUT)
            if response.status_code == HTTPStatus.CREATED:
                return True, "newly flagged"
            if response.status_code == HTTPStatus.CONFLICT:
                return True, "already flagged on server"
            if response.status_code == HTTPStatus.UNAUTHORIZED:
                return False, "unauthorized (valid session cookie required)"
            if response.status_code == HTTPStatus.NOT_FOUND:
                return False, "post not found on pawchive"
        except requests.RequestException as exc:
            last_error = str(exc)

    return False, f"failed to flag: {last_error}"


def get_latest_patreon_posts(
    session: requests.Session,
    campaign_id: str,
    count: int = 20,
) -> list[dict]:
    """Fetch recent public post metadata directly from Patreon campaign API.

    Args:
        session: Active requests HTTP session.
        campaign_id: Patreon numeric campaign ID.
        count: Number of recent posts to retrieve.

    Returns:
        List of dicts containing 'id', 'title', 'published', and 'url'.
    """
    url = (
        f"{PATREON_API_BASE}/posts?filter[campaign_id]={campaign_id}"
        f"&sort=-published_at&page[count]={count}"
    )

    # Query Patreon campaign posts endpoint.
    for _ in range(RETRIES + 1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            items = response.json().get("data", [])
            results = []

            for item in items:
                attrs = item.get("attributes", {})
                pub_str = attrs.get("published_at")
                if not pub_str:
                    continue

                pub_dt = parse_date(pub_str)
                default_post_url = f"https://www.patreon.com/posts/{item.get('id')}"
                post_url = str(attrs.get("url") or default_post_url)

                results.append({
                    "id": str(item.get("id")),
                    "title": str(attrs.get("title") or "Untitled"),
                    "published": pub_dt,
                    "url": post_url,
                })

            return results
        except (requests.RequestException, json.JSONDecodeError):
            pass

    return []


def sync_and_flag_patreon_posts(
    session: requests.Session,
    feed: dict,
    newest_retained_date: dt.datetime | None,
    known_imported_ids: set[str] | None = None,
) -> list[dict]:
    """Detect newer Patreon posts missing from Pawchive and politely request re-import.

    Flow:
        1. Fetch latest posts directly from Patreon campaign API.
        2. Identify posts that are newer than latest retained item and not imported yet.
        3. Submit polite flag requests for identified missing posts.

    Args:
        session: Authenticated requests HTTP session.
        feed: Feed configuration dictionary.
        newest_retained_date: Timestamp of newest existing post in the local/remote feed.
        known_imported_ids: Optional set of post IDs known to be imported on Pawchive.

    Returns:
        List of newly flagged post dictionaries.
    """
    campaign_id = feed.get("campaign_id")
    if not campaign_id:
        return []

    # Fetch recent posts from Patreon campaign API.
    patreon_posts = get_latest_patreon_posts(session, campaign_id)
    if not patreon_posts:
        return []

    # Filter for posts not yet imported and strictly newer than retained items.
    known_ids = known_imported_ids or set()
    newer_posts = [
        p
        for p in patreon_posts
        if p["id"] not in known_ids
        and (newest_retained_date is None or p["published"] > newest_retained_date)
    ]
    if not newer_posts:
        return []

    # Flag missing posts sequentially on Pawchive.
    service = feed.get("service", "patreon")
    creator_id = feed["creator_id"]
    for post in sorted(newer_posts, key=lambda x: x["published"]):
        _, reason = flag_post_politely(session, service, creator_id, post["id"])
        print(f"  [patreon sync] Post {post['id']} ({post['title']}): {reason}")

    return newer_posts


def run_feed(
    session: requests.Session,
    feed: dict,
    sync_mode: SyncMode = SyncMode.SYNC_PATREON,
) -> int:
    """Execute complete feed build, merge, and synchronization pipeline for a creator.

    Args:
        session: Active HTTP session.
        feed: Feed configuration dictionary.
        sync_mode: SyncMode indicating whether to check Patreon after building feed.

    Returns:
        Number of items retained in the final feed.
    """
    name = feed["fallback_name"]
    print(f"[{feed['key']}] {name}")

    # Load existing historical items and fetch latest creator posts from Pawchive.
    existing = load_existing(session, feed)
    posts = collect_posts(session, feed["creator_id"])

    # Merge new posts into historical collection and write feed artifacts.
    items, rendered, evicted = merge_new_posts(existing, posts)
    count = len(items)
    write_feed(feed, name, build_feed_xml(feed, name, items), count)

    print(
        f"  listing items: {len(posts)}; retained: {len(existing)}; "
        f"newly rendered: {rendered}; evicted oldest: {evicted}; feed items: {count}"
    )

    # If enabled, check Patreon for newer posts not yet archived on Pawchive.
    if sync_mode == SyncMode.SYNC_PATREON:
        newest_date = max((val[0] for val in items.values()), default=None)
        known_imported_ids = {
            str(p.get("id"))
            for p in posts
            if isinstance(p, dict) and p.get("id")
        }
        sync_and_flag_patreon_posts(session, feed, newest_date, known_imported_ids)

    return count


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for building Pawchive RSS feeds and managing post flags.

    Args:
        argv: Optional list of command-line argument strings.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    # Parse CLI flags and configuration options.
    parser = argparse.ArgumentParser(
        description="Build Pawchive RSS feeds and manage re-import flags.",
    )
    parser.add_argument(
        "--flag",
        metavar="ID_OR_URL",
        help="Check and politely flag a specific post for re-import",
    )
    parser.add_argument(
        "--check-flag",
        metavar="ID_OR_URL",
        help="Check if a specific post is flagged on Pawchive",
    )
    parser.add_argument(
        "--check-patreon",
        action="store_true",
        help="Check Patreon for newer posts without building feeds",
    )
    parser.add_argument(
        "--skip-patreon-sync",
        action="store_true",
        help="Skip checking Patreon during feed build",
    )
    parser.add_argument(
        "--session",
        metavar="COOKIE",
        help="Override Pawchive session cookie",
    )
    opts = parser.parse_args(argv)

    # Initialize authenticated session with cookie override if provided.
    cookie = opts.session if opts.session else SESSION_COOKIE
    session = make_session(cookie=cookie)

    # Handle post flag query action (--check-flag).
    if opts.check_flag:
        service, creator_id, post_id = parse_post_identifier(opts.check_flag)
        is_flagged = check_post_flag(session, service, creator_id, post_id)
        status_text = "FLAGGED (in re-import queue)" if is_flagged else "NOT flagged"
        print(f"Post {post_id} on {service}/{creator_id}: {status_text}")
        return 0

    # Handle post flag submission action (--flag).
    if opts.flag:
        service, creator_id, post_id = parse_post_identifier(opts.flag)
        ok, reason = flag_post_politely(session, service, creator_id, post_id)
        print(f"Post {post_id} on {service}/{creator_id}: {reason}")
        return 0 if ok else 1

    # Handle Patreon check dry-run action (--check-patreon).
    if opts.check_patreon:
        for feed in FEEDS:
            campaign_id = feed.get("campaign_id")
            if not campaign_id:
                continue
            existing = load_existing(session, feed)
            newest_date = max((val[0] for val in existing.values()), default=None)
            newer = sync_and_flag_patreon_posts(session, feed, newest_date)
            print(f"[{feed['key']}] Found {len(newer)} newer post(s) on Patreon.")
        return 0

    # Ensure output directory exists and prune obsolete feed subdirectories.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pruned = prune_untracked_feeds(OUT_DIR, {feed["key"] for feed in FEEDS})
    if pruned:
        print(f"Pruned untracked Pawchive feed(s): {', '.join(pruned)}")

    # Execute feed generation for all configured creators.
    sync_mode = (
        SyncMode.SKIP_PATREON
        if opts.skip_patreon_sync
        else SyncMode.SYNC_PATREON
    )
    counts = {
        feed["key"]: run_feed(session, feed, sync_mode=sync_mode)
        for feed in FEEDS
    }

    # Write root index.html listing links to all generated feed endpoints.
    links = "".join(
        f"<li><a href='{html.escape(feed['key'])}/feed.xml'>"
        f"{html.escape(feed['fallback_name'])}</a></li>"
        for feed in FEEDS
    )
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Pawchive RSS feeds</title>"
        f"<h1>Pawchive RSS feeds</h1><ul>{links}</ul>",
        encoding="utf-8",
    )
    print("Done:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
