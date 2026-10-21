# Miscellaneous full-text RSS feeds

This repository follows the standalone-builder structure of `pib_feed`: each
top-level Python script owns one source and writes its generated files below
`public/<feed-key>/`. Source-agnostic plumbing (HTTP retries, date coercion,
RSS rendering, merge/eviction, feed writing, and pruning) lives in
`rss_common.py`; the builders stay thin source-specific adapters.

## Pawchive Patreon feeds

`pawchive_feed.py` publishes full-text RSS 2.0 feeds for selected Patreon creators
archived by Pawchive:

| Feed | Novel | Creator / Author | GitHub Pages |
|------|-------|------------------|--------------|
| cerim/hdt | Hell Difficulty Tutorial | Patreon `31891971` (Cerim) | [feed.xml](https://jumpingpony.github.io/misc_feed/cerim/hdt/feed.xml) |
| void/thr | The Hundred Reigns | Patreon `16493499` (Void Herald) | [feed.xml](https://jumpingpony.github.io/misc_feed/void/thr/feed.xml) |
| onedayokay/tac | The Archmage Coefficient | Patreon `207666427` (onedayokay) | [feed.xml](https://jumpingpony.github.io/misc_feed/onedayokay/tac/feed.xml) |
| meister7slop | The Strongest Spellblade | Patreon `64285829` (MisterVii) | [feed.xml](https://jumpingpony.github.io/misc_feed/meister7slop/feed.xml) |

Post discovery and complete post objects come from the
[Pawchive v1 API](https://pawchive.pw/api/schema). Chapter extraction uses the
same post-content field and unknown-tag handling as WebToEpub's
`PawchiveParser`, then removes Patreon previous/next navigation and attachments.
Author notes, their links, and separators remain part of the chapter. The
remaining chapter HTML is stored in each item's `<content:encoded>` element.

Each feed retains the 30 newest chapter posts. A per-feed title pattern decides
what counts as a chapter: `cerim/hdt` keeps titles containing "chapter",
`void/thr` keeps numbered `The Hundred Reigns <n>: ...` posts, `onedayokay/tac`
keeps posts starting with an optional `Chapter ` prefix and a chapter number,
and `meister7slop` keeps `SB: Chapter <n>` posts, so polls, art posts, and
announcements are dropped. Pawchive enforces 50-result API pages, so the
builder requests only the first page for each creator. It loads the published
RSS first, renders only absent posts whose publication time is newer than the
newest retained item, merges them by permalink, and trims the oldest items only
after merging. It does not crawl listing HTML or fetch individual post pages.

### Post Arrival Schedule & Patreon Sync

For `cerim/hdt` (Patreon `31891971`):
- **Active Schedule**: 5 days a week (**Sunday through Thursday**).
- **Publication Times**: Typically between **08:00 – 09:30 UTC** (**13:30 – 15:00 IST**), averaging around **08:35 UTC / 14:05 IST**.

During each build run (scheduled every 6 hours at 00:25 / 06:25 / 12:25 / 18:25 UTC, 05:55 / 11:55 / 17:55 / 23:55 IST), `pawchive_feed.py` automatically checks Patreon's public API for newly published posts. If a newer post exists on Patreon that has not yet been ingested by Pawchive, it performs a **polite check-before-set** re-import request:
1. Queries `GET /api/v1/patreon/user/{creator_id}/post/{post_id}/flag`.
2. If already flagged, it skips politely without making write requests.
3. If unflagged, it sends `POST /api/v1/patreon/user/{creator_id}/post/{post_id}/flag` using the session cookie.

## Local run

```bash
python3 -m pip install -r requirements.txt
python3 pawchive_feed.py
```

### CLI Flagging Utilities

```bash
# Check flag status on Pawchive (public, accepts post ID or Patreon/Pawchive URLs)
python3 pawchive_feed.py --check-flag 167183185
python3 pawchive_feed.py --check-flag https://www.patreon.com/cerim/posts/167183185

# Check Patreon for newer posts and politely flag any missing ones (also checks cum.st)
python3 pawchive_feed.py --check-patreon

# Check cum.st for newer posts
python3 pawchive_feed.py --check-cumst

# Explicitly check-and-flag a specific post or URL
python3 pawchive_feed.py --flag 167183185

# Build feeds without checking Patreon and cum.st
python3 pawchive_feed.py --skip-patreon-sync
```

Output:

- `public/cerim/hdt/feed.xml`
- `public/void/thr/feed.xml`
- `public/onedayokay/tac/feed.xml`
- `public/meister7slop/feed.xml`

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PAWCHIVE_TIMEOUT` | `30` | HTTP timeout in seconds |
| `PAWCHIVE_RETRIES` | `2` | Retries after the initial request |
| `PAWCHIVE_MAX_ITEMS` | `30` | Maximum retained items per feed |
| `PAWCHIVE_OUT_DIR` | `public` | Generated output directory |
| `PAWCHIVE_SITE_BASE_URL` | unset | Deployed site root used for RSS self-links |
| `PAWCHIVE_SESSION` | hardcoded session | Pawchive login session cookie used for authenticated flag requests |

The GitHub Actions workflow runs every 6 hours (00:25, 06:25, 12:25 and
18:25 UTC), on pushes to `main` that touch the builders, and on demand, then
deploys `public/` to GitHub Pages.
Importable feed collections are available in `OPML/`.

## Royal Road fiction feeds

`royalroad_feed.py` publishes full-text RSS feeds for selected Royal Road
fiction.

Currently no Royal Road feeds are configured.

Royal Road's official syndication feeds truncate chapter bodies. The builder
uses them only to discover the newest 10 chapters, then extracts complete
`div.chapter-inner` content from chapter pages. Following WebToEpub's
`RoyalRoadParser`, it removes CSS-hidden watermark elements, empty images,
previous/next navigation, legacy `<-->` navigation separators, and Royal Road
author-note portlets.

The published RSS is loaded before discovery. Existing chapters are merged by
stable link/GUID, and only absent chapters newer than the newest retained item
are fetched and rendered. The item cap is applied after merging, so new chapters
displace the oldest retained chapters only. The first backfill spaces chapter
requests one second apart.

Run locally with `python3 royalroad_feed.py`.

Royal Road configuration:

| Variable | Default | Meaning |
|----------|---------|---------|
| `ROYALROAD_TIMEOUT` | `30` | HTTP timeout in seconds |
| `ROYALROAD_RETRIES` | `2` | Retries after the initial request |
| `ROYALROAD_MAX_ITEMS` | `10` | Maximum retained items per feed |
| `ROYALROAD_REQUEST_DELAY` | `1` | Delay between newly fetched chapter pages |
| `ROYALROAD_OUT_DIR` | `public` | Generated output directory |
| `ROYALROAD_SITE_BASE_URL` | unset | Deployed site root used for self-links and body reuse |

## ePubLibre English additions

`epublibre_feed.py` tracks the English-language branch of the ePubLibre
catalogue (language id `5`) and publishes the newest books as one feed:

| Feed | Content | GitHub Pages |
|------|---------|--------------|
| `epublibre/en` | English-language books added to ePubLibre | [feed.xml](https://jumpingpony.github.io/misc_feed/epublibre/en/feed.xml) |

Discovery uses the catalogue's AJAX endpoint
(`/catalogo/index/{offset}/nuevo/todos/sin/5/--/ajax`), which returns 18
newest-first tiles per page. Each new book costs one detail-page request that
supplies the date added (`En la web desde`), synopsis, collections, genres,
and the magnet link. Items carry the cover, metadata, synopsis, and download
link in `content:encoded`.

The feed keeps at most 100 books, and only books added in the last 28 days;
older entries are dropped on the next build. Existing items are loaded from
the local feed and the deployed copy before discovery, and a published id
ends the walk because the listing is newest-first. A steady-state build costs
one catalogue request. Requests are spaced 7 seconds apart.

ePubLibre answers throttled requests with an empty `200` body. The builder
treats a page without the metadata table as a failure, retries after
`EPUBLIBRE_BLOCK_DELAY`, and aborts the build rather than publish an entry
with a guessed date or missing magnet.

| Variable | Default | Meaning |
|----------|---------|---------|
| `EPUBLIBRE_MAX_ITEMS` | `100` | Maximum retained items in the feed |
| `EPUBLIBRE_FEED_DAYS` | `28` | Only keep books added within this many days |
| `EPUBLIBRE_BACKFILL_PAGES` | `6` | Catalogue pages scanned on a cold start |
| `EPUBLIBRE_TIMEOUT` | `30` | HTTP timeout in seconds |
| `EPUBLIBRE_RETRIES` | `2` | Retries after the initial request |
| `EPUBLIBRE_REQUEST_DELAY` | `7` | Seconds between requests |
| `EPUBLIBRE_BLOCK_DELAY` | `300` | Seconds to wait after an empty (throttled) page |
| `EPUBLIBRE_OUT_DIR` | `public` | Generated output directory |
| `EPUBLIBRE_SITE_BASE_URL` | unset | Deployed site root used for RSS self-links |

## Caveats

- The project is unofficial and unaffiliated with Pawchive or Patreon.
- Feed availability depends on Pawchive's API and archive contents.
- Posts are published only when their titles match the feed's pattern; a
  chapter whose title breaks the pattern is dropped.
- The Royal Road feed depends on its syndication and chapter-page structures;
  it is unofficial and unaffiliated with Royal Road or its author.
