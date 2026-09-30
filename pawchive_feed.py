#!/usr/bin/env python3
"""Build full-text RSS feeds for selected Pawchive Patreon creators.

Post discovery and post bodies come from Pawchive's documented v1 API.  Chapter
HTML is read from the same API field used by WebToEpub's PawchiveParser. Explicit
previous/next navigation and attachments are omitted; author notes remain.

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.  Shared
plumbing lives in rss_common.py; this builder keeps source-specific logic.
"""
from __future__ import annotations

import argparse
import datetime as dt
from enum import Enum, auto
import html
from html.parser import HTMLParser
from http import HTTPStatus
import os
from pathlib import Path
import re

import requests

import rss_common as rss


BASE = "https://pawchive.pw"
API_BASE = BASE + "/api/v1"
PATREON_BASE = "https://www.patreon.com"
PATREON_API_BASE = PATREON_BASE + "/api"
CUMST_BASE = "https://cum.st"
CUMST_API_BASE = CUMST_BASE + "/api/v1"

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
        "title": "Hell Difficulty Tutorial",
        "author": "Cerim",
    },
    {
        "key": "void-herald",
        "creator_id": "16493499",
        "campaign_id": "2369856",
        "fallback_name": "Void Herald",
        "title": "The Hundred Reigns",
        "author": "Void Herald",
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
    active_cookie = cookie if cookie is not None else SESSION_COOKIE

    # Attach the session cookie for authenticated endpoints when available.
    return rss.make_session(
        cookie=active_cookie,
        cookie_domain="pawchive.pw",
        accept=rss.AcceptFormat.JSON,
    )


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
            if name in rss.URL_ATTRS:
                value = rss.safe_url(value, self.base_url)
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
    """Extract and sanitize the complete chapter HTML body for RSS embedding.

    Args:
        post: Post dictionary retrieved from Pawchive API.

    Returns:
        Sanitized HTML string; CDATA escaping happens at render time.
    """
    post_url = post_permalink(post)

    # Run the raw HTML content through navigation cleanup and tag allowlist filtering.
    sanitizer = ChapterSanitizer(post_url)
    sanitizer.feed(remove_chapter_fluff(str(post.get("content") or "")))
    sanitizer.close()

    return sanitizer.get_html()


def parse_date(value: object) -> dt.datetime:
    """Parse ISO 8601 string or numeric unix timestamp into UTC datetime.

    Args:
        value: Date representation (ISO string, unix epoch number, datetime,
            or None).

    Returns:
        Timezone-aware datetime in UTC.  Already-parsed datetimes pass through;
        missing or invalid values default to the current time.
    """
    return rss.parse_iso_datetime(value)


def post_permalink(post: dict) -> str:
    """Build the canonical Patreon chapter link for a given post.

    Example:
        >>> post = {"id": "167183185"}
        >>> post_permalink(post)
        'https://www.patreon.com/posts/167183185'

    Args:
        post: Post dictionary containing 'id' and optional 'url'.

    Returns:
        Canonical Patreon chapter URL string.
    """
    url = post.get("url")
    if isinstance(url, str) and url.startswith(PATREON_BASE):
        return url

    post_id = post.get("id", "")
    return f"{PATREON_BASE}/posts/{post_id}"


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

    return rss.render_rss_item(
        title=title,
        link=link,
        guid=link,
        published=published,
        body=build_chapter(post),
        guid_kind=rss.GuidKind.PERMALINK,
    )


ITEM_RE = re.compile(r"<item>.*?</item>", re.S)
GUID_RE = re.compile(r"<guid[^>]*>(.*?)</guid>", re.S)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


def extract_post_id(value: str) -> str | None:
    """Extract numeric post ID from a Patreon or Pawchive URL or ID string.

    Args:
        value: URL or identifier string.

    Returns:
        Extracted numeric post ID string or None.
    """
    m = re.search(r"/posts?/(\d+)", value)
    if m:
        return m.group(1)

    digits = re.search(r"\b\d+\b", value)
    if digits:
        return digits.group(0)

    return None


def parse_existing_items(document: str) -> dict[str, tuple[dt.datetime, str]]:
    """Extract existing rendered <item> XML blocks from an RSS document without re-parsing HTML.

    Args:
        document: Full XML document string of a previously generated feed.

    Returns:
        Dictionary mapping item identity (canonical Patreon URL) to a tuple of
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

        published = rss.parse_rfc822_datetime(date_match.group(1))
        if published is None:
            continue

        raw_identity = html.unescape(identity_match.group(1)).strip()
        if not raw_identity:
            continue

        # Canonicalize to Patreon post permalink for uniform de-duplication across platforms.
        post_id = extract_post_id(raw_identity)
        identity = f"{PATREON_BASE}/posts/{post_id}" if post_id else raw_identity
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
    documents = rss.load_feed_documents(
        session,
        out_dir=OUT_DIR,
        key=feed["key"],
        site_base_url=SITE_BASE_URL,
        timeout=TIMEOUT,
        retries=RETRIES,
    )

    # Merge items across all loaded documents (local and remote), tolerating BOMs.
    items: dict[str, tuple[dt.datetime, str]] = {}
    for document in documents:
        items.update(parse_existing_items(document.decode("utf-8-sig")))

    return items


def collect_posts(
    session: requests.Session,
    creator_id: str,
    campaign_id: str | None = None,
) -> list[dict]:
    """Fetch and merge latest posts from Pawchive and cum.st.

    Pawchive provides primary post archives. If a chapter appears on cum.st first,
    it is extracted and unified into the feed candidates without duplication.

    Args:
        session: Active requests HTTP session.
        creator_id: Pawchive creator user ID.
        campaign_id: Optional Patreon/cum.st campaign ID.

    Returns:
        List of post dictionaries sorted newest first up to ITEM_LIMIT.
    """
    posts_by_id: dict[str, dict] = {}

    # Query cum.st first if campaign ID is configured.
    if campaign_id:
        cumst_url = f"{CUMST_API_BASE}/patreon/user/{campaign_id}/posts?limit={ITEM_LIMIT}"
        try:
            cumst_data = rss.fetch_json(session, cumst_url, timeout=TIMEOUT, retries=RETRIES)
            cumst_items = cumst_data.get("posts", []) if isinstance(cumst_data, dict) else []
            for item in cumst_items:
                pid = str(item.get("id") or "")
                body = item.get("captionHtml") or item.get("caption") or ""
                if not pid or not body:
                    continue

                posts_by_id[pid] = {
                    "id": pid,
                    "title": str(item.get("title") or "Untitled"),
                    "published": parse_date(item.get("published")),
                    "content": body,
                    "user": creator_id,
                    "service": "patreon",
                    "url": f"{PATREON_BASE}/posts/{pid}",
                }
        except (RuntimeError, requests.RequestException):
            pass

    # Query Pawchive API.
    url = f"{API_BASE}/patreon/user/{creator_id}?o=0"
    try:
        payload = rss.fetch_json(session, url, timeout=TIMEOUT, retries=RETRIES)
        if isinstance(payload, list):
            for post in payload:
                if not isinstance(post, dict) or not post.get("id"):
                    continue

                pid = str(post["id"])
                existing_entry = posts_by_id.get(pid, {})
                post_content = post.get("content") or existing_entry.get("content") or ""

                posts_by_id[pid] = {
                    "id": pid,
                    "title": str(post.get("title") or existing_entry.get("title") or "Untitled"),
                    "published": parse_date(post.get("published") or post.get("added")),
                    "content": post_content,
                    "user": creator_id,
                    "service": "patreon",
                    "url": post.get("url") or f"{PATREON_BASE}/posts/{pid}",
                }
        elif not posts_by_id:
            raise RuntimeError(f"unexpected creator-post response from {url}")
    except (RuntimeError, requests.RequestException):
        if not posts_by_id:
            raise

    # Sort posts descending by publication date and retain top slice.
    posts = list(posts_by_id.values())
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

    Args:
        existing: Mapping of identity -> (published_date, item_xml).
        posts: List of freshly fetched post dictionaries.
        renderer: Callable converting a post dict to an item XML string.

    Returns:
        Tuple of (merged_items_dict, newly_rendered_count, evicted_count).
    """
    # Canonicalize retained identities to post IDs for robust de-duplication.
    existing_guids = {
        post_id
        for key in existing
        if (post_id := extract_post_id(key)) is not None
    }

    return rss.merge_new_items(
        existing,
        posts,
        identity=post_permalink,
        published=lambda post: parse_date(post.get("published") or post.get("added")),
        load=lambda post: renderer(post).strip(),
        limit=ITEM_LIMIT,
        guid=lambda post: str(post.get("id") or "") or None,
        existing_guids=existing_guids,
    )


# Format feed title as 'novel title - Author'.
def feed_title(feed: dict) -> str:
    return rss.feed_title(feed)


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
    title = feed_title(feed) or f"{name} — Pawchive"
    description = f"Unofficial full-text feed for {title} archived by Pawchive."
    self_url = f"{SITE_BASE_URL}/{feed['key']}/feed.xml" if SITE_BASE_URL else None

    return rss.build_feed_document(
        title=title,
        link=creator_url,
        description=description,
        item_xml=(item for _, item in ordered),
        self_url=self_url,
    )


def write_feed(feed: dict, name: str, xml: str, count: int) -> None:
    """Write feed.xml and companion HTML index page to output directory.

    Args:
        feed: Feed configuration dictionary.
        name: Display name of the creator.
        xml: Generated RSS XML document string.
        count: Number of items contained in the feed.
    """
    # Write feed.xml and the companion index.html landing page.
    title = feed_title(feed) or f"{name} — Pawchive"
    rss.write_feed_files(
        OUT_DIR,
        feed["key"],
        xml,
        title=title,
        page_suffix="Pawchive RSS",
        description="Unofficial full-text feed generated from the Pawchive API.",
        count=count,
    )


def is_pawchive_feed_dir(path: Path) -> bool:
    """Verify whether a directory contains files generated by the Pawchive feed builder.

    Args:
        path: Path to directory in OUT_DIR.

    Returns:
        True if the directory contains a feed.xml or index.html mentioning Pawchive signatures.
    """
    return rss.directory_has_signature(
        path,
        feed_markers=("pawchive.pw", "Pawchive"),
        page_markers=("Pawchive",),
    )


def prune_untracked_feeds(out_dir: Path, active_keys: set[str]) -> list[str]:
    """Delete output directories belonging to Pawchive feeds that are no longer configured.

    Args:
        out_dir: Output root directory path.
        active_keys: Set of active feed key strings from FEEDS.

    Returns:
        Sorted list of pruned directory names.
    """
    return rss.prune_untracked_feeds(out_dir, active_keys, is_pawchive_feed_dir)


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

    # A 404 means "not flagged"; other persistent failures are treated as unset.
    try:
        response = rss.fetch(
            session,
            url,
            timeout=TIMEOUT,
            retries=RETRIES,
            on_not_found=rss.OnNotFound.RETURN_NONE,
        )
    except RuntimeError:
        return False

    return response is not None and response.status_code == HTTPStatus.OK


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

    # Query the Patreon campaign posts endpoint, tolerating transient failures.
    try:
        payload = rss.fetch_json(session, url, timeout=TIMEOUT, retries=RETRIES)
    except RuntimeError:
        return []

    items = payload.get("data", []) if isinstance(payload, dict) else []
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


def get_latest_cumst_posts(
    session: requests.Session,
    campaign_id: str,
    count: int = 20,
) -> list[dict]:
    """Fetch recent post metadata from cum.st creator feed API.

    Args:
        session: Active requests HTTP session.
        campaign_id: Creator campaign ID on cum.st.
        count: Maximum number of posts to retrieve.

    Returns:
        List of dicts containing 'id', 'title', 'published', and 'url'.
    """
    url = f"{CUMST_API_BASE}/patreon/user/{campaign_id}/posts?limit={count}"

    # Query the cum.st posts endpoint, tolerating transient failures.
    try:
        payload = rss.fetch_json(session, url, timeout=TIMEOUT, retries=RETRIES)
    except RuntimeError:
        return []

    items = payload.get("posts", []) if isinstance(payload, dict) else []
    results = []

    for item in items:
        pub_val = item.get("published")
        if pub_val is None:
            continue

        pub_dt = parse_date(pub_val)
        post_id = str(item.get("id"))
        post_url = f"{CUMST_BASE}/patreon/user/{campaign_id}/post/{post_id}"

        results.append({
            "id": post_id,
            "title": str(item.get("title") or "Untitled"),
            "published": pub_dt,
            "url": post_url,
        })

    return results


def check_cumst_posts(
    session: requests.Session,
    feed: dict,
    newest_retained_date: dt.datetime | None,
    known_imported_ids: set[str] | None = None,
) -> list[dict]:
    """Check cum.st for posts missing from the local Pawchive feed.

    Args:
        session: Active HTTP session.
        feed: Feed configuration dictionary.
        newest_retained_date: Timestamp of newest existing post in feed.
        known_imported_ids: Set of post IDs already known to Pawchive.

    Returns:
        List of newer post dictionaries found on cum.st.
    """
    campaign_id = feed.get("campaign_id")
    if not campaign_id:
        return []

    # Fetch recent posts from cum.st API.
    cumst_posts = get_latest_cumst_posts(session, campaign_id)
    if not cumst_posts:
        return []

    # Filter for posts not yet imported and newer than retained items.
    known_ids = known_imported_ids or set()
    newer_posts = [
        p
        for p in cumst_posts
        if p["id"] not in known_ids
        and (newest_retained_date is None or p["published"] > newest_retained_date)
    ]

    # Report cum.st availability.
    for post in sorted(newer_posts, key=lambda x: x["published"]):
        print(f"  [cum.st check] Post {post['id']} ({post['title']}): available on cum.st")

    return newer_posts


def get_feed_sync_baseline(
    session: requests.Session,
    feed: dict,
) -> tuple[dt.datetime | None, set[str]]:
    """Determine latest publication date and known post IDs from disk and Pawchive.

    Args:
        session: Active HTTP session.
        feed: Feed configuration dictionary.

    Returns:
        Tuple of (newest_retained_date, known_imported_ids).
    """
    existing = load_existing(session, feed)
    posts = collect_posts(session, feed["creator_id"], feed.get("campaign_id"))

    # Extract all known post IDs from Pawchive listing.
    known_ids = {
        str(p.get("id"))
        for p in posts
        if isinstance(p, dict) and p.get("id")
    }

    # Identify most recent timestamp between disk cache and Pawchive listing.
    all_dates = [val[0] for val in existing.values()] + [
        parse_date(p.get("published") or p.get("added"))
        for p in posts
        if isinstance(p, dict)
    ]
    newest_date = max(all_dates, default=None)

    return newest_date, known_ids


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
    print(f"[{feed['key']}] {feed_title(feed) or name}")

    # Load existing historical items and fetch latest creator posts from Pawchive.
    existing = load_existing(session, feed)
    posts = collect_posts(session, feed["creator_id"], feed.get("campaign_id"))

    # Merge new posts into historical collection and write feed artifacts.
    items, rendered, evicted = merge_new_posts(existing, posts)
    count = len(items)
    write_feed(feed, name, build_feed_xml(feed, name, items), count)

    print(
        f"  listing items: {len(posts)}; retained: {len(existing)}; "
        f"newly rendered: {rendered}; evicted oldest: {evicted}; feed items: {count}"
    )

    # If enabled, check Patreon and cum.st for newer posts not yet archived on Pawchive.
    if sync_mode == SyncMode.SYNC_PATREON:
        newest_date = max((val[0] for val in items.values()), default=None)
        known_imported_ids = {
            str(p.get("id"))
            for p in posts
            if isinstance(p, dict) and p.get("id")
        }
        sync_and_flag_patreon_posts(session, feed, newest_date, known_imported_ids)
        check_cumst_posts(session, feed, newest_date, known_imported_ids)

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
        "--check-cumst",
        action="store_true",
        help="Check cum.st for newer posts without building feeds",
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

    # Handle cum.st check dry-run action (--check-cumst).
    if opts.check_cumst:
        for feed in FEEDS:
            campaign_id = feed.get("campaign_id")
            if not campaign_id:
                continue
            newest_date, known_ids = get_feed_sync_baseline(session, feed)
            newer = check_cumst_posts(session, feed, newest_date, known_ids)
            print(f"[{feed['key']}] Found {len(newer)} newer post(s) on cum.st.")
        return 0

    # Handle Patreon check dry-run action (--check-patreon).
    if opts.check_patreon:
        for feed in FEEDS:
            campaign_id = feed.get("campaign_id")
            if not campaign_id:
                continue
            newest_date, known_ids = get_feed_sync_baseline(session, feed)
            newer = sync_and_flag_patreon_posts(session, feed, newest_date, known_ids)
            print(f"[{feed['key']}] Found {len(newer)} newer post(s) on Patreon.")
            newer_cumst = check_cumst_posts(session, feed, newest_date, known_ids)
            print(f"[{feed['key']}] Found {len(newer_cumst)} newer post(s) on cum.st.")
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
        f"{html.escape(feed_title(feed) or feed['fallback_name'])}</a></li>"
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
