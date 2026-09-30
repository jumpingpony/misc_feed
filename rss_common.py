#!/usr/bin/env python3
"""Shared plumbing for the standalone full-text RSS feed builders.

The repository keeps one top-level builder per source (see DOCS.md), but the
source-agnostic pieces are the same across builders: HTTP session setup and
retrying fetches, URL and text sanitizing helpers, date coercion, RSS item and
channel rendering, feed-file writing, and stale feed-directory pruning.  This
module owns those pieces so each builder only contains source-specific logic.
"""
from __future__ import annotations

import datetime as dt
from email.utils import format_datetime, parsedate_to_datetime
import html
from http import HTTPStatus
import json
from pathlib import Path
import re
import shutil
from typing import Callable, Iterable, TypeVar
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import requests


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.7922.76 Safari/537.36"
)
UTC = dt.timezone.utc
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
URL_ATTRS = {"href", "src"}

T = TypeVar("T")
C = TypeVar("C")


def make_session(
    *,
    cookie: str | None = None,
    cookie_domain: str | None = None,
    accept_json: bool = False,
) -> requests.Session:
    """Create an HTTP session with browser-like headers and an optional cookie.

    Args:
        cookie: Optional session cookie value for authenticated endpoints.
        cookie_domain: Cookie domain; required for the cookie to be attached.
        accept_json: Send ``Accept: application/json`` when true.

    Returns:
        Configured requests.Session.
    """
    session = requests.Session()

    # Set default browser headers for API and HTML requests.
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    if accept_json:
        headers["Accept"] = "application/json"
    session.headers.update(headers)

    # Attach session cookie when available for authenticated endpoints.
    if cookie and cookie_domain:
        session.cookies.set("session", cookie, domain=cookie_domain)

    return session


def fetch(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    retries: int,
    optional: bool = False,
) -> requests.Response | None:
    """Fetch an HTTP resource with retry handling and optional 404 tolerance.

    Args:
        session: Active requests HTTP session.
        url: HTTP/HTTPS endpoint URL to query.
        timeout: Per-attempt timeout in seconds.
        retries: Retries after the initial attempt.
        optional: Return None on HTTP 404 instead of raising.

    Returns:
        Response on success, or None for an optional resource that is absent.

    Raises:
        RuntimeError: If every attempt fails.
    """
    last_error = "unknown error"

    # Retry transient network and throttling failures.
    for _ in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout)

            # A missing optional resource means "not published yet".
            if optional and response.status_code == HTTPStatus.NOT_FOUND:
                return None

            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = str(exc)

    # Exhausted retry count; raise a descriptive error.
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    retries: int,
) -> object:
    """Fetch and decode JSON with retries on network and decode failures.

    Args:
        session: Active requests HTTP session.
        url: JSON endpoint URL to query.
        timeout: Per-attempt timeout in seconds.
        retries: Retries after the initial attempt.

    Returns:
        Decoded JSON payload.

    Raises:
        RuntimeError: If every attempt fails.
    """
    last_error = "unknown error"

    # Retry transient network and JSON decode failures.
    for _ in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = str(exc)

    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def safe_url(value: str, base_url: str) -> str | None:
    """Resolve a relative URL against a base URL and enforce allowed schemes.

    Args:
        value: Raw href or src attribute value.
        base_url: Base URL used to resolve relative paths.

    Returns:
        Absolute URL when the scheme is http, https, or mailto; None otherwise.
    """
    absolute = urljoin(base_url, value.strip())

    # Allow only web and mail protocols; block script execution vectors.
    if urlparse(absolute).scheme.lower() in {"http", "https", "mailto"}:
        return absolute

    return None


def parse_iso_datetime(value: object) -> dt.datetime:
    """Coerce a datetime, unix timestamp, or ISO-8601 string into a UTC datetime.

    Args:
        value: Date representation (datetime, ISO string, unix epoch number,
            or None).

    Returns:
        Timezone-aware UTC datetime.  Datetimes pass through unchanged so
        already-parsed values are not replaced with the current time; missing
        or unparseable values default to the current time.
    """
    if isinstance(value, dt.datetime):
        return value

    # Convert numeric unix timestamp (from platforms like cum.st) to datetime.
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, UTC)

    # Attempt ISO-8601 string parsing when valid string input is provided.
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            pass

    # Fallback to current time if the date field was missing or invalid.
    return dt.datetime.now(UTC)


def parse_rfc822_datetime(value: str | None) -> dt.datetime | None:
    """Parse an RFC-822 / RFC-2822 date string from an RSS feed document.

    Args:
        value: Date string (e.g., 'Tue, 04 Aug 2026 21:00:16 +0000').

    Returns:
        Timezone-aware UTC datetime, or None if parsing fails.
    """
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(html.unescape(str(value)).strip())
    except (TypeError, ValueError):
        return None

    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def plain_summary(body: str, limit: int = 500) -> str:
    """Create a plain-text excerpt from chapter HTML for the RSS <description> tag.

    Args:
        body: Chapter HTML content string.
        limit: Maximum character length of the summary before truncation.

    Returns:
        Plain-text summary truncated cleanly at the last space boundary.
    """
    # Strip HTML tags and collapse whitespace runs into single spaces.
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text).strip()

    # Truncate text at word boundary if it exceeds the length limit.
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"

    return text


def render_rss_item(
    *,
    title: str,
    link: str,
    guid: str,
    published: dt.datetime,
    body: str,
    guid_is_permalink: bool = True,
) -> str:
    """Render one RSS 2.0 <item> block with a full-text content payload.

    Args:
        title: Item title.
        link: Item permalink.
        guid: Stable item identifier.
        published: Publication timestamp.
        body: Full chapter HTML body.
        guid_is_permalink: Whether the GUID doubles as a permalink.

    Returns:
        Complete <item>...</item> XML block string.
    """
    # XML CDATA blocks cannot contain the sequence ']]>'.
    # Splitting into ']]]]><![CDATA[>' preserves the literal characters in XML parsers.
    cdata_body = body.replace("]]>", "]]]]><![CDATA[>")
    permalink = "true" if guid_is_permalink else "false"

    return (
        "    <item>\n"
        f"      <title>{escape(title)}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f"      <guid isPermaLink=\"{permalink}\">{escape(guid)}</guid>\n"
        f"      <pubDate>{format_datetime(published)}</pubDate>\n"
        f"      <description>{escape(plain_summary(body))}</description>\n"
        f"      <content:encoded><![CDATA[{cdata_body}]]></content:encoded>\n"
        "    </item>"
    )


def build_feed_document(
    *,
    title: str,
    link: str,
    description: str,
    item_xml: Iterable[str],
    self_url: str | None = None,
) -> str:
    """Assemble a complete RSS 2.0 feed document.

    Args:
        title: Channel title.
        link: Channel link.
        description: Channel description.
        item_xml: Rendered <item> XML blocks, newest first.
        self_url: Optional deployed feed URL for the atom:link self reference.

    Returns:
        Complete UTF-8 encoded RSS 2.0 XML document string.
    """
    # Build the optional self-referencing atom:link header.
    self_link = (
        f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml" />\n'
        if self_url
        else ""
    )

    # Assemble the complete RSS XML document.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(link)}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{format_datetime(dt.datetime.now(UTC))}</lastBuildDate>\n"
        f"{self_link}"
        + "\n".join(item_xml)
        + "\n  </channel>\n</rss>\n"
    )


def feed_title(feed: dict) -> str:
    """Format a feed title as 'novel title - Author'.

    Args:
        feed: Feed configuration with optional 'title', 'author', and
            'fallback_name' entries.

    Returns:
        Formatted title, or whichever component is available.
    """
    title = feed.get("title")
    author = feed.get("author") or feed.get("fallback_name")
    if title and author:
        return f"{title} - {author}"

    return title or author or ""


def write_feed_files(
    out_dir: Path,
    key: str,
    xml: str,
    *,
    title: str,
    page_suffix: str,
    description: str,
    count: int,
) -> None:
    """Write feed.xml and a companion HTML landing page below out_dir/key.

    Args:
        out_dir: Output root directory path.
        key: Feed key used as the target subdirectory name.
        xml: Generated RSS XML document string.
        title: Feed title used on the landing page.
        page_suffix: Builder name appended to the page title and heading.
        description: Landing-page description line.
        count: Number of items contained in the feed.
    """
    # Create the target feed output directory.
    directory = out_dir / key
    directory.mkdir(parents=True, exist_ok=True)

    # Write the feed.xml file.
    (directory / "feed.xml").write_text(xml, encoding="utf-8")

    # Generate and write the companion index.html landing page.
    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — {html.escape(page_suffix)}</title>"
        f"<h1>{html.escape(title)} — {html.escape(page_suffix)}</h1>"
        f"<p>{html.escape(description)}</p>"
        "<p><a href='feed.xml'>Subscribe to feed.xml</a></p>"
        f"<p>{count} items.</p>"
    )
    (directory / "index.html").write_text(page, encoding="utf-8")


def load_feed_documents(
    session: requests.Session,
    *,
    out_dir: Path,
    key: str,
    site_base_url: str,
    timeout: int,
    retries: int,
) -> list[bytes]:
    """Load local and optionally deployed copies of a published feed.

    Args:
        session: Active requests HTTP session.
        out_dir: Output root directory containing generated feeds.
        key: Feed key used as the subdirectory name.
        site_base_url: Deployed site root, or an empty string to skip the remote.
        timeout: Per-attempt timeout in seconds.
        retries: Retries after the initial attempt.

    Returns:
        Raw feed documents found on disk and, when configured, remotely.
    """
    documents: list[bytes] = []

    # Read the locally cached feed XML file if it exists on disk.
    local_path = out_dir / key / "feed.xml"
    if local_path.is_file():
        documents.append(local_path.read_bytes())

    # Fetch the live feed to retain historical items on fresh CI checkouts.
    if site_base_url:
        response = fetch(
            session,
            f"{site_base_url}/{key}/feed.xml",
            timeout=timeout,
            retries=retries,
            optional=True,
        )
        if response is not None:
            documents.append(response.content)

    return documents


def merge_new_items(
    existing: dict[str, tuple[dt.datetime, T]],
    candidates: list[C],
    *,
    identity: Callable[[C], str],
    published: Callable[[C], dt.datetime],
    load: Callable[[C], T],
    limit: int,
    guid: Callable[[C], str | None] | None = None,
    existing_guids: set[str] | None = None,
) -> tuple[dict[str, tuple[dt.datetime, T]], int, int]:
    """Merge freshly discovered items into a retained feed with dedup and eviction.

    Args:
        existing: Mapping of identity -> (published_date, rendered_value).
        candidates: List of freshly fetched candidate items.
        identity: Callable returning the candidate merge key.
        published: Callable returning the candidate publication timestamp.
        load: Callable producing the rendered value for a new candidate.
        limit: Maximum number of retained items.
        guid: Optional callable returning a candidate GUID for deduplication.
        existing_guids: GUIDs already present in the retained feed.

    Returns:
        Tuple of (merged_mapping, newly_loaded_count, evicted_count).  The
        mapping is ordered newest first and capped at the item limit.
    """
    merged = dict(existing)
    newest_existing = max((date for date, _ in existing.values()), default=None)
    known_guids = set(existing_guids or ())
    loaded = 0

    # Add only unseen items strictly newer than the newest retained entry.
    for candidate in candidates:
        key = identity(candidate)
        if key in merged:
            continue

        candidate_guid = guid(candidate) if guid else None
        if candidate_guid and candidate_guid in known_guids:
            continue

        when = published(candidate)
        if newest_existing is not None and when <= newest_existing:
            continue

        merged[key] = (when, load(candidate))
        if candidate_guid:
            known_guids.add(candidate_guid)
        loaded += 1

    # Sort merged items newest first and evict the oldest beyond the cap.
    ordered = sorted(merged.items(), key=lambda item: (item[1][0], item[0]), reverse=True)
    evicted = max(0, len(ordered) - limit)

    return dict(ordered[:limit]), loaded, evicted


def directory_has_signature(
    path: Path,
    *,
    feed_markers: Iterable[str],
    page_markers: Iterable[str],
    exclude: Iterable[str] = (),
) -> bool:
    """Check whether a directory holds feed files with expected source markers.

    Args:
        path: Candidate feed directory below the output root.
        feed_markers: Markers identifying the source in feed.xml.
        page_markers: Markers identifying the source in index.html.
        exclude: Markers that disqualify the directory (other builders).

    Returns:
        True when feed.xml or index.html matches the requested markers.
    """
    if not path.is_dir():
        return False

    # Check feed.xml first, then index.html as a fallback signature.
    for name, markers in (("feed.xml", feed_markers), ("index.html", page_markers)):
        feed_file = path / name
        if not feed_file.is_file():
            continue
        try:
            content = feed_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker in content for marker in exclude):
            continue
        if any(marker in content for marker in markers):
            return True

    return False


def prune_untracked_feeds(
    out_dir: Path,
    active_keys: set[str],
    is_feed_dir: Callable[[Path], bool],
) -> list[str]:
    """Delete output directories belonging to feeds that are no longer configured.

    Args:
        out_dir: Output root directory path.
        active_keys: Set of active feed key strings.
        is_feed_dir: Predicate identifying directories owned by the builder.

    Returns:
        Sorted list of pruned directory names.
    """
    if not out_dir.is_dir():
        return []

    # Iterate through child directories and remove obsolete feeds.
    removed: list[str] = []
    for child in out_dir.iterdir():
        if child.is_dir() and child.name not in active_keys and is_feed_dir(child):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as err:
                print(f"Failed to remove untracked feed directory {child}: {err}")

    return sorted(removed)
