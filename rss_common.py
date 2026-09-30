#!/usr/bin/env python3
"""Shared plumbing for the standalone full-text RSS feed builders.

One top-level builder per source lives at the repository root (see DOCS.md);
the source-agnostic pieces below are shared so each builder only carries
source-specific logic.
"""
from __future__ import annotations

import datetime as dt
from email.utils import format_datetime, parsedate_to_datetime
from enum import Enum, auto
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
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
UTC = dt.timezone.utc
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ATOM_NS = "http://www.w3.org/2005/Atom"
URL_ATTRS = {"href", "src"}

FEED_FILE = "feed.xml"
PAGE_FILE = "index.html"
COOKIE_NAME = "session"
LANGUAGE = "en"
SUMMARY_LIMIT = 500
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'
RSS_VERSION = "2.0"

T = TypeVar("T")
C = TypeVar("C")


class AcceptFormat(Enum):
    # Accept header advertised on session requests; value is the header string.
    DEFAULT = ""
    JSON = "application/json"


class OnNotFound(Enum):
    # Fetch behavior when the server answers HTTP 404.
    RETURN_NONE = auto()
    RAISE = auto()


class GuidKind(Enum):
    # RSS isPermaLink attribute value for the item GUID.
    PERMALINK = "true"
    IDENTIFIER = "false"


def make_session(
    *,
    cookie: str | None = None,
    cookie_domain: str | None = None,
    accept: AcceptFormat = AcceptFormat.DEFAULT,
) -> requests.Session:
    """HTTP session with browser-like headers and an optional auth cookie."""
    headers = {"User-Agent": UA, "Accept-Language": LANGUAGE}
    if accept is not AcceptFormat.DEFAULT:
        headers["Accept"] = accept.value

    session = requests.Session()
    session.headers.update(headers)

    if cookie and cookie_domain:
        session.cookies.set(COOKIE_NAME, cookie, domain=cookie_domain)

    return session


def fetch(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    retries: int,
    on_not_found: OnNotFound = OnNotFound.RAISE,
) -> requests.Response | None:
    """Fetch one URL with retries; 404 can mean 'not published yet'."""
    last_error = "unknown error"

    # Retry transient network and throttling failures.
    for _ in range(retries + 1):
        try:
            response = session.get(url, timeout=timeout)

            if (
                on_not_found is OnNotFound.RETURN_NONE
                and response.status_code == HTTPStatus.NOT_FOUND
            ):
                return None

            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = str(exc)

    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_json(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    retries: int,
) -> object:
    """Fetch and decode one JSON URL with retries."""
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
    """Resolve a relative URL; keep only web and mail schemes."""
    absolute = urljoin(base_url, value.strip())

    # Other schemes can execute script or point at local resources.
    if urlparse(absolute).scheme.lower() not in {"http", "https", "mailto"}:
        return None

    return absolute


def parse_iso_datetime(value: object) -> dt.datetime:
    """Coerce datetime, unix timestamp, or ISO-8601 string to UTC datetime.

    Parsed datetimes pass through untouched so they keep their publication
    time; missing or invalid values fall back to now.
    """
    if isinstance(value, dt.datetime):
        return value

    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, UTC)

    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    return dt.datetime.now(UTC)


def parse_rfc822_datetime(value: str | None) -> dt.datetime | None:
    """Parse an RFC-822 pubDate from an existing RSS document."""
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(html.unescape(str(value)).strip())
    except (TypeError, ValueError):
        return None

    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _summary(body: str, limit: int = SUMMARY_LIMIT) -> str:
    """Plain-text excerpt for the RSS <description> tag."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"\s+", " ", text).strip()

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
    guid_kind: GuidKind = GuidKind.PERMALINK,
) -> str:
    """Render one RSS 2.0 <item> with a full-text content:encoded payload."""
    # CDATA cannot contain ']]>'; split it so the text survives parsing.
    cdata_body = body.replace("]]>", "]]]]><![CDATA[>")

    return (
        "    <item>\n"
        f"      <title>{escape(title)}</title>\n"
        f"      <link>{escape(link)}</link>\n"
        f'      <guid isPermaLink="{guid_kind.value}">{escape(guid)}</guid>\n'
        f"      <pubDate>{format_datetime(published)}</pubDate>\n"
        f"      <description>{escape(_summary(body))}</description>\n"
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
    """Assemble a complete RSS 2.0 document."""
    self_link = (
        f'    <atom:link href="{escape(self_url)}" rel="self" '
        'type="application/rss+xml" />\n'
        if self_url
        else ""
    )

    return (
        XML_DECLARATION
        + f'<rss version="{RSS_VERSION}" xmlns:content="{CONTENT_NS}" '
        f'xmlns:atom="{ATOM_NS}">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(link)}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        f"    <language>{LANGUAGE}</language>\n"
        f"    <lastBuildDate>{format_datetime(dt.datetime.now(UTC))}</lastBuildDate>\n"
        f"{self_link}"
        + "\n".join(item_xml)
        + "\n  </channel>\n</rss>\n"
    )


def feed_title(feed: dict) -> str:
    """Format feed title as 'novel title - Author'."""
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
    """Write feed.xml and a companion HTML landing page below out_dir/key."""
    directory = out_dir / key
    directory.mkdir(parents=True, exist_ok=True)
    (directory / FEED_FILE).write_text(xml, encoding="utf-8")

    page = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — {html.escape(page_suffix)}</title>"
        f"<h1>{html.escape(title)} — {html.escape(page_suffix)}</h1>"
        f"<p>{html.escape(description)}</p>"
        f"<p><a href='{FEED_FILE}'>Subscribe to {FEED_FILE}</a></p>"
        f"<p>{count} items.</p>"
    )
    (directory / PAGE_FILE).write_text(page, encoding="utf-8")


def load_feed_documents(
    session: requests.Session,
    *,
    out_dir: Path,
    key: str,
    site_base_url: str,
    timeout: int,
    retries: int,
) -> list[bytes]:
    """Load local and, when configured, deployed copies of a published feed."""
    documents: list[bytes] = []

    local_path = out_dir / key / FEED_FILE
    if local_path.is_file():
        documents.append(local_path.read_bytes())

    # The remote copy retains history on fresh CI checkouts.
    if site_base_url:
        response = fetch(
            session,
            f"{site_base_url}/{key}/{FEED_FILE}",
            timeout=timeout,
            retries=retries,
            on_not_found=OnNotFound.RETURN_NONE,
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
    """Merge fresh candidates into retained items with dedup and eviction."""
    merged = dict(existing)
    newest_existing = max((date for date, _ in existing.values()), default=None)
    known_guids = set(existing_guids or ())
    loaded = 0

    # Only unseen items strictly newer than the newest retained entry join.
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

    # Keep newest-first order and cap at limit.
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
    """Check whether a directory holds feed files with source markers."""
    if not path.is_dir():
        return False

    # feed.xml carries the strongest signature; index.html is the fallback.
    for name, markers in ((FEED_FILE, feed_markers), (PAGE_FILE, page_markers)):
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
    """Delete output directories of feeds no longer configured."""
    if not out_dir.is_dir():
        return []

    removed: list[str] = []
    for child in out_dir.iterdir():
        if child.is_dir() and child.name not in active_keys and is_feed_dir(child):
            try:
                shutil.rmtree(child)
                removed.append(child.name)
            except OSError as err:
                print(f"Failed to remove untracked feed directory {child}: {err}")

    return sorted(removed)
