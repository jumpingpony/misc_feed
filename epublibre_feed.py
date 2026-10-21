#!/usr/bin/env python3
"""Build an RSS feed of English-language additions to ePubLibre.

ePubLibre is a Spanish catalogue that also distributes English editions
(language id 5).  The catalogue lists them newest-first, and every detail
page carries the date the book entered the site, its synopsis, and a magnet
link.

Pipeline:
  +---------------------------+     +---------------------------+
  |  /catalogo/.../5/--/ajax  | --> |  Parse 18 catalogue tiles |
  |  (newest English first)   |     |  (id, title, author, ...) |
  +---------------------------+     +---------------------------+
                                                |
                                                v
  +---------------------------+     +---------------------------+
  |  Published feed.xml       | --> |  Diff by detail URL       |
  |  (local + GitHub Pages)   |     |  (stop at first known)    |
  +---------------------------+     +---------------------------+
                                                |
                                                v
                                     +---------------------------+
                                     |  Fetch new detail pages   |
                                     |  (date, synopsis, magnet) |
                                     +---------------------------+
                                                |
                                                v
                                     +---------------------------+
                                     |  Write feed.xml + index   |
                                     +---------------------------+

Requests are spaced by EPUBLIBRE_REQUEST_DELAY seconds (default 7).  A
steady-state build costs one catalogue request; a cold start walks up to six
catalogue pages and one detail page per book added in the last four weeks.
The feed keeps at most EPUBLIBRE_MAX_ITEMS (default 100) entries.

Output: public/<feed-key>/feed.xml and public/<feed-key>/index.html.
"""
from __future__ import annotations

import datetime as dt
import html
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import requests

import rss_common as rss


BASE = "https://www.epublibre.org"
LANGUAGE_ID = "5"  # inglés
PAGE_SIZE = 18
RENDER_VERSION = "2"  # bump to force re-rendering of retained items

FEEDS: list[dict[str, str]] = [
    {
        "key": "epublibre/en",
        "title": "ePubLibre English additions",
        "author": "",
    }
]

ITEM_LIMIT = max(1, int(os.environ.get("EPUBLIBRE_MAX_ITEMS", "100")))
TIMEOUT = max(1, int(os.environ.get("EPUBLIBRE_TIMEOUT", "30")))
RETRIES = max(0, int(os.environ.get("EPUBLIBRE_RETRIES", "2")))
REQUEST_DELAY = max(0.0, float(os.environ.get("EPUBLIBRE_REQUEST_DELAY", "7")))
BLOCK_DELAY = max(0, int(os.environ.get("EPUBLIBRE_BLOCK_DELAY", "300")))
BACKFILL_PAGES = max(1, int(os.environ.get("EPUBLIBRE_BACKFILL_PAGES", "6")))
FEED_DAYS = max(1, int(os.environ.get("EPUBLIBRE_FEED_DAYS", "28")))
OUT_DIR = Path(os.environ.get("EPUBLIBRE_OUT_DIR", "public"))
SITE_BASE_URL = os.environ.get("EPUBLIBRE_SITE_BASE_URL", "").strip().rstrip("/")

# Monotonic timestamp of the previous request, shared by every fetch helper.
_last_request = 0.0


def make_session() -> requests.Session:
    """Create a requests session with the shared browser-like headers."""
    return rss.make_session()


def pause() -> None:
    """Sleep until REQUEST_DELAY seconds have passed since the last request."""
    global _last_request
    if _last_request:
        elapsed = time.monotonic() - _last_request
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
    _last_request = time.monotonic()


def catalog_url(offset: int) -> str:
    """Return the AJAX catalogue URL for one page of English books."""
    return (
        f"{BASE}/catalogo/index/{offset}/nuevo/todos/sin/"
        f"{LANGUAGE_ID}/--/ajax"
    )


def detail_url(book_id: str) -> str:
    """Return the public detail-page URL for one ePubLibre book id."""
    return f"{BASE}/libro/detalle/{book_id}"


def fetch_listing(session: requests.Session, offset: int) -> list[dict]:
    """Fetch one catalogue page and return its parsed book tiles."""
    url = catalog_url(offset)
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE}/catalogo/index/1/nuevo/todos/sin/{LANGUAGE_ID}",
    }
    last_error = "unknown error"

    # AJAX endpoint answers JSON; retry transient failures and bad payloads.
    for attempt in range(RETRIES + 1):
        pause()
        try:
            response = session.post(url, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            return parse_listing(payload.get("contenido", ""))
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            if attempt < RETRIES:
                time.sleep(BLOCK_DELAY)

    raise RuntimeError(f"failed to fetch catalogue page at offset {offset}: {last_error}")


def parse_listing(document: str) -> list[dict]:
    """Parse catalogue tiles into id, title, author, revision, pages, cover."""
    soup = BeautifulSoup(document, "html.parser")
    books = []

    for anchor in soup.select("a.popover-libro"):
        match = re.search(r"/libro/detalle/(\d+)", anchor.get("href", ""))
        if match is None:
            continue

        book_id = match.group(1)
        popover = anchor.get("data-content", "")
        image = anchor.find("img")
        state = re.search(r"estado-(\d+)", str(anchor))
        author = BeautifulSoup(popover, "html.parser").select_one("img.user_silhouette")
        author_name = ""
        if author is not None and isinstance(author.next_sibling, str):
            author_name = author.next_sibling.strip()

        books.append(
            {
                "id": book_id,
                "link": detail_url(book_id),
                "title": (anchor.get("title") or "").strip(),
                "author": author_name,
                "revision": _first(r'icon-revision"[^>]*/>\s*([\d.]+)', popover),
                "pages": _first(r'icon-paginas"[^>]*/>\s*(\d+)', popover),
                "cover": image.get("src", "") if image is not None else "",
                "state": state.group(1) if state else "",
            }
        )

    return books


def fetch_book(session: requests.Session, book: dict) -> dict | None:
    """Fetch one detail page and merge its fields into the listing entry.

    Some catalogue entries (provisional or restricted books) answer with an
    empty 200 body.  Those return None so the caller can skip them; network
    failures still raise.
    """
    pause()
    response = rss.fetch(session, book["link"], timeout=TIMEOUT, retries=RETRIES)
    assert response is not None

    if len(response.content) < 1024 or "En la web desde" not in response.text:
        print(f"  skip empty detail page: {book['link']}")
        return None

    return {**book, **parse_detail(response.text, book)}


def parse_detail(document: str, book: dict) -> dict:
    """Extract synopsis, date added, metadata, and magnet link from a detail page."""
    soup = BeautifulSoup(document, "html.parser")
    detail = soup.select_one("div.detalle") or soup

    synopsis = detail.select_one("div.ali_justi")
    magnet = soup.select_one("a#en_desc")
    info = soup.find(string=re.compile("En la web desde"))
    info_text = (
        re.sub(r"\s+", " ", info.find_parent("table").get_text(" ", strip=True))
        if info is not None
        else ""
    )

    cover = soup.select_one("img#portada")
    added = _first(r"En la web desde:\s*(\d{2}-\d{2}-\d{4})", info_text)

    # Keep writers only; illustrators and translators sit in the same table.
    writers = []
    for row in detail.select("table tr"):
        role = _text(row.select_one("div.label")).lower()
        if role in {"escritor", "escritora", "autor", "autora"}:
            writers.extend(_text(a) for a in row.select("a[href*='/autor/index/']"))

    writers = list(dict.fromkeys(name for name in writers if name))
    fallback = [
        _text(a) for a in detail.select("a[href*='/autor/index/']")
    ]
    fallback = list(dict.fromkeys(name for name in fallback if name))

    return {
        "title": _text(soup.select_one("div.titulo")) or book["title"],
        "author": ", ".join(writers) or ", ".join(fallback) or book["author"],
        "cover": (cover.get("src", "") if cover is not None else "")
        or book["cover"],
        "synopsis": _plain_text(synopsis) if synopsis is not None else "",
        "collections": [
            _text(link) for link in detail.select("a[href*='/coleccion/index/']")
        ],
        "genres": [_text(link) for link in detail.select("a[href*='/genero/index/']")],
        "pages": _first(r"Páginas:\s*(\d+)", info_text) or book["pages"],
        "year": _first(r"Publicado en:\s*(\d{4})", info_text),
        "revision": _first(r"Revisión:\s*([\d.]+)", info_text) or book["revision"],
        "state": _first(r"Estado:\s*([A-Za-zÁÉÍÓÚáéíóúñ]+)", info_text),
        "added": added,
        "date": parse_added(added),
        "magnet": magnet.get("href", "") if magnet is not None else "",
    }


def cutoff() -> dt.datetime:
    """Return midnight UTC of the oldest day still inside the feed window."""
    today = dt.datetime.now(rss.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return today - dt.timedelta(days=FEED_DAYS)


def parse_added(value: str) -> dt.datetime:
    """Convert the site's dd-mm-yyyy date into a UTC datetime.

    ePubLibre publishes the day only; midnight UTC keeps the ordering stable
    and avoids a tzdata dependency on Windows CI.
    """
    if value:
        try:
            return dt.datetime.strptime(value, "%d-%m-%Y").replace(tzinfo=rss.UTC)
        except ValueError:
            pass

    return dt.datetime.now(rss.UTC)


def item_body(book: dict) -> str:
    """Render the HTML body of one book: cover, facts, synopsis, magnet."""
    link = book["link"]
    parts = []

    # Cover links back to the detail page.
    cover = rss.safe_url(book.get("cover", ""), book["link"]) if book.get("cover") else None
    if cover:
        parts.append(
            f'<p><a href="{html.escape(link)}">'
            f'<img src="{html.escape(cover)}" alt="Cover" /></a></p>'
        )

    if book.get("author"):
        parts.append(f"<p><strong>{html.escape(book['author'])}</strong></p>")

    facts = [
        ("Pages", book.get("pages")),
        ("Published", book.get("year")),
        ("Revision", book.get("revision")),
        ("Status", book.get("state")),
    ]
    fact_line = " &middot; ".join(
        f"{label}: {html.escape(value)}" for label, value in facts if value
    )
    if fact_line:
        parts.append(f"<p>{fact_line}</p>")

    if book.get("synopsis"):
        parts.append(_paragraphs(book["synopsis"]))

    if book.get("collections"):
        names = ", ".join(html.escape(name) for name in book["collections"])
        parts.append(f"<p>Collections: {names}</p>")

    if book.get("genres"):
        names = ", ".join(html.escape(name) for name in book["genres"])
        parts.append(f"<p>Genres: {names}</p>")

    # Magnet links are EPL's own distribution channel; only btih is accepted.
    magnet = book.get("magnet", "")
    if magnet.startswith("magnet:?xt=urn:btih:"):
        parts.append(
            f'<p><a href="{html.escape(magnet)}">Download (magnet link)</a><br/>'
            f'<a href="{html.escape(link)}">ePubLibre page</a></p>'
        )

    return "\n".join(parts)


def render_item(item: dict) -> str:
    """Render one retained book as an RSS <item> block."""
    return rss.render_rss_item(
        title=item["title"],
        link=item["link"],
        guid=item["link"],
        published=item["date"],
        body=item["body_html"],
    )


def load_existing(session: requests.Session, feed: dict) -> dict[str, dict]:
    """Load published items from the local feed and its deployed copy."""
    documents = rss.load_feed_documents(
        session,
        out_dir=OUT_DIR,
        key=feed["key"],
        site_base_url=SITE_BASE_URL,
        timeout=TIMEOUT,
        retries=RETRIES,
    )

    items: dict[str, dict] = {}
    for document in documents:
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            continue

        # Feeds rendered by an older template are ignored so items rebuild.
        description = root.findtext("./channel/description") or ""
        if f"render v{RENDER_VERSION}" not in description:
            continue

        for item in root.findall("./channel/item"):
            link = (item.findtext("link") or "").strip()
            body = item.findtext(f"{{{rss.CONTENT_NS}}}encoded") or ""
            if not link or not body:
                continue
            items[link] = {
                "title": (item.findtext("title") or "Untitled").strip(),
                "link": link,
                "date": rss.parse_rfc822_datetime(item.findtext("pubDate"))
                or dt.datetime.now(rss.UTC),
                "body_html": body,
            }

    return items


def collect_new(session: requests.Session, existing: dict[str, dict]) -> list[dict]:
    """Fetch unseen catalogue entries until the four-week window is left.

    The listing is newest-first, so the first already-published or expired
    book ends the walk.  Detail pages are fetched only for books that are
    about to enter the feed.
    """
    start = cutoff()
    fresh: list[dict] = []
    blanks = 0

    for page in range(BACKFILL_PAGES):
        listing = fetch_listing(session, page * PAGE_SIZE)
        if not listing:
            if page == 0:
                raise RuntimeError("empty catalogue page; refusing to publish an empty feed")
            break

        for book in listing:
            if book["link"] in existing:
                return fresh

            detail = fetch_book(session, book)
            if detail is None:
                # A run of blank pages means throttling, not a bad entry.
                blanks += 1
                if blanks >= 3:
                    raise RuntimeError("detail pages are empty; site is throttling")
                continue

            blanks = 0
            if detail["date"] < start:
                return fresh

            fresh.append(detail)
            if len(fresh) >= ITEM_LIMIT:
                return fresh

        if len(listing) < PAGE_SIZE:
            break

    return fresh


def build_feed_xml(feed: dict, items: list[dict]) -> str:
    """Assemble the RSS document for one feed."""
    self_url = f"{SITE_BASE_URL}/{feed['key']}/feed.xml" if SITE_BASE_URL else None

    return rss.build_feed_document(
        title=feed["title"],
        link=f"{BASE}/catalogo/index/0/nuevo/todos/sin/{LANGUAGE_ID}",
        description=(
            "Newest English-language books added to ePubLibre, "
            f"with synopsis and magnet link. Unofficial. (render v{RENDER_VERSION})"
        ),
        item_xml=(render_item(item) for item in items),
        self_url=self_url,
    )


def write_feed(feed: dict, xml: str, count: int) -> None:
    """Write feed.xml and the companion index.html."""
    rss.write_feed_files(
        OUT_DIR,
        feed["key"],
        xml,
        title=feed["title"],
        page_suffix="ePubLibre RSS",
        description="Unofficial feed of new English-language ePubLibre books.",
        count=count,
    )


def is_epublibre_feed_dir(path: Path) -> bool:
    """Check whether a directory holds files generated by this builder."""
    return rss.directory_has_signature(
        path,
        feed_markers=("epublibre.org", "ePubLibre"),
        page_markers=("ePubLibre",),
        exclude=("pawchive.pw", "royalroad.com"),
    )


def prune_untracked_feeds(out_dir: Path, active_keys: set[str]) -> list[str]:
    """Delete output directories of feeds no longer configured."""
    return rss.prune_untracked_feeds(out_dir, active_keys, is_epublibre_feed_dir)


def run_feed(session: requests.Session, feed: dict) -> int:
    """Execute the full build pipeline for the ePubLibre feed."""
    print(f"[{feed['key']}] {feed['title']}")

    # Load history, then fetch details only for unseen books inside the window.
    existing = load_existing(session, feed)
    fresh = collect_new(session, existing)

    merged, fetched, evicted = rss.merge_new_items(
        {link: (item["date"], item) for link, item in existing.items()},
        fresh,
        identity=lambda book: book["link"],
        published=lambda book: book["date"],
        load=lambda book: {
            "title": (
                f"{book['title']} - {book['author']}"
                if book.get("author")
                else book["title"]
            ),
            "link": book["link"],
            "date": book["date"],
            "body_html": item_body(book),
        },
        limit=ITEM_LIMIT,
        require_newer=False,
    )

    # Existing items that fell out of the window are dropped, not re-published.
    items = [
        item for _, item in merged.values() if item["date"] >= cutoff()
    ]
    write_feed(feed, build_feed_xml(feed, items), len(items))

    print(
        f"  new books: {len(fresh)}; retained: {len(existing)}; "
        f"fetched: {fetched}; evicted: {evicted}; feed items: {len(items)}"
    )

    return len(items)


def main() -> int:
    """Build the feed and prune untracked ePubLibre output directories."""
    session = make_session()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pruned = prune_untracked_feeds(OUT_DIR, {feed["key"] for feed in FEEDS})
    if pruned:
        print(f"Pruned untracked ePubLibre feed(s): {', '.join(pruned)}")

    counts = {feed["key"]: run_feed(session, feed) for feed in FEEDS}
    print("Done:", counts)

    return 0


def _first(pattern: str, text: str) -> str:
    """Return the first capture group of a pattern, or an empty string."""
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def _text(node) -> str:
    """Return collapsed text for a node, or an empty string."""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _plain_text(node) -> str:
    """Convert synopsis HTML into paragraphs separated by blank lines."""
    soup = BeautifulSoup(str(node), "html.parser")

    # <br> is the site's only block separator inside a synopsis.
    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _paragraphs(text: str) -> str:
    """Escape plain text and rebuild <p> paragraphs with <br> line breaks."""
    paragraphs = [block for block in text.split("\n\n") if block.strip()]

    return "\n".join(
        "<p>" + html.escape(block).replace("\n", "<br/>") + "</p>"
        for block in paragraphs
    )


if __name__ == "__main__":
    raise SystemExit(main())
