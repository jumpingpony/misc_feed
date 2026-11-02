# Miscellaneous full-text RSS feeds

This repository follows the standalone-builder structure of `pib_feed`: each
top-level Python script owns one source and writes its generated files below
`public/<feed-key>/`. Source-agnostic plumbing (HTTP retries, date coercion,
RSS rendering, merge/eviction, feed writing, and pruning) lives in
`rss_common.py`; the builders stay thin source-specific adapters.

## Pawchive Patreon feeds

`pawchive_feed.py` publishes full-text RSS 2.0 feeds for selected Patreon creators:

| Feed | Novel | Creator / Author | GitHub Pages |
|------|-------|------------------|--------------|
| cerim/hdt | Hell Difficulty Tutorial | Patreon `31891971` (Cerim) | [feed.xml](https://jumpingpony.github.io/misc_feed/cerim/hdt/feed.xml) |

Chapter bodies come from Patreon's web API first. A patron session cookie
(`PATREON_COOKIE`) authenticates the account, and every post the account can
read in full is rendered as soon as Patreon publishes it. cum.st and Pawchive
remain fallbacks: a post the direct source cannot serve is taken from cum.st,
then from Pawchive, and a slower archive never replaces a direct body.

Patreon publishes a post roughly three hours before Pawchive's archive ingests
it, so the direct source is what keeps the feed current. Chapter extraction
uses the same HTML and unknown-tag handling as WebToEpub's `PawchiveParser`,
then removes Patreon previous/next navigation and attachments. Author notes,
their links, and separators remain part of the chapter. The remaining chapter
HTML is stored in each item's `<content:encoded>` element.

Each feed retains the 30 newest chapter posts. A per-feed title pattern decides
what counts as a chapter: `cerim/hdt` keeps titles containing "chapter", so
polls, art posts, and announcements are dropped. Pawchive enforces 50-result API
pages, so the builder requests only the first page for each creator. It loads the
published RSS first, renders only absent posts whose publication time is newer than the
newest retained item, merges them by canonical Patreon post ID, and trims the oldest items only
after merging. It does not crawl listing HTML or fetch individual post pages.

### Post arrival schedule and release watch

For `cerim/hdt` (Patreon `31891971`), measured over 49 chapters from August to
November 2026:

- **Active Schedule**: 5 days a week (**Sunday through Thursday**).
- **Publication Times**: **10:23 – 10:59 Europe/Berlin**, with occasional
  earlier releases (09:13 was the earliest seen). That is 08:23 – 08:59 UTC
  during CEST and 09:23 – 09:59 UTC during CET.

GitHub's scheduler does not fire on time: scheduled runs are routinely delayed
5–30 minutes. The workflow therefore starts `patreon_watch.py` once a day at
06:45 UTC (08:45 CEST / 07:45 CET). The watcher:

- exits at once on Friday and Saturday, which are outside Cerim's Sunday–Thursday schedule;
- sleeps until **08:50 Europe/Berlin**;
- polls Patreon post metadata every four minutes;
- stops as soon as the newest chapter is missing from the published feed, or when today's chapter is already uploaded (one scheduled chapter per day).

The build job then rebuilds and deploys, normally within five minutes of
publication. The watcher reads only public metadata and needs no credential.

Daylight saving is handled explicitly: the window is defined in Europe/Berlin
and converted with the EU rules (summer time starts the last Sunday of March at
01:00 UTC and ends the last Sunday of October at 01:00 UTC). The UTC window is
therefore 06:50–09:05 in summer and 07:50–10:05 in winter, and it moves on the
transition days without a code change. The 06:45 UTC start is always after the
01:00 UTC switch, so the same day's offset is already the new one.

The six-hourly builds remain as a backstop for the other feeds and for any day
the watcher cannot run.

### Patreon session cookie

Create the repository secret once:

```bash
gh secret set PATREON_COOKIE
```

Paste the **`session_id` value** (not `__cf_bm`, not the whole cookie header):
DevTools → Application → Cookies → `https://www.patreon.com` → `session_id`.
A full `Cookie` header also works. The workflow passes the secret only to the
feed-build step; the watcher never receives it.

Patreon issues `session_id` with a **one-year expiry** and does not rotate it
on API calls, so normal builds do not invalidate it. It dies early only when
the account signs out of Patreon, changes the password, or revokes the session.
When Patreon rejects it, the build opens a **"Patreon cookie expired or
missing"** issue, keeps deploying from Pawchive/cum.st, and closes the issue
automatically once a refreshed secret authenticates again. That is one manual
update per year, not per chapter.

During each build run, `pawchive_feed.py` also checks Patreon's public API for
newly published posts. If a newer post exists on Patreon that Pawchive has not
ingested yet, it performs a **polite check-before-set** re-import request:
1. Queries `GET /api/v1/patreon/user/{creator_id}/post/{post_id}/flag`.
2. If already flagged, it skips politely without making write requests.
3. If unflagged, it sends `POST /api/v1/patreon/user/{creator_id}/post/{post_id}/flag` using the session cookie.

## Local run

```bash
python3 -m pip install -r requirements.txt

# Optional: read chapter bodies straight from Patreon.
export PATREON_COOKIE='session_id=...'

python3 pawchive_feed.py
python3 patreon_watch.py --once   # one metadata check, no waiting
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

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PAWCHIVE_TIMEOUT` | `30` | HTTP timeout in seconds |
| `PAWCHIVE_RETRIES` | `2` | Retries after the initial request |
| `PAWCHIVE_MAX_ITEMS` | `30` | Maximum retained items per feed |
| `PAWCHIVE_OUT_DIR` | `public` | Generated output directory |
| `PAWCHIVE_SITE_BASE_URL` | unset | Deployed site root used for RSS self-links |
| `PAWCHIVE_SESSION` | hardcoded session | Pawchive login session cookie used for authenticated flag requests |
| `PATREON_COOKIE` | unset | Patreon `Cookie` header for the patron account; set as the `PATREON_COOKIE` repository secret |
| `PATREON_WATCH_INTERVAL` | `240` | Seconds between watcher polls inside the release window |
| `PATREON_WATCH_DAYS` | `0,1,2,3,6` | Weekdays with a release (Monday=0); default is Sunday–Thursday |

The GitHub Actions workflow builds every 6 hours (00:25, 06:25, 12:25 and
18:25 UTC), once a day before the release window (06:45 UTC, with the watcher
waiting for 08:50 Europe/Berlin), on pushes to `main` that touch the builders,
and on demand, then deploys `public/` to GitHub Pages.
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
supplies the listing date (`En la web desde`, or `Actualizado el` on revised
books), synopsis, collections, genres, and the magnet link. Items carry the
cover, metadata, synopsis, and download link in `content:encoded`.

The feed keeps at most 100 books, and only books added in the last 28 days;
older entries are dropped on the next build. Existing items are loaded from
the local feed and the deployed copy before discovery, and a published id
ends the walk because the listing is newest-first. A steady-state build costs
one catalogue request. Requests are spaced 7 seconds apart.

Some catalogue entries (provisional or restricted books) answer with an
empty `200` body; the builder skips those entries. Three blank detail pages
in a row are treated as throttling and abort the build, so a blocked run
never publishes a partial feed. A failed catalogue request is retried after
`EPUBLIBRE_BLOCK_DELAY`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `EPUBLIBRE_MAX_ITEMS` | `100` | Maximum retained items in the feed |
| `EPUBLIBRE_FEED_DAYS` | `28` | Only keep books added within this many days |
| `EPUBLIBRE_BACKFILL_PAGES` | `6` | Catalogue pages scanned on a cold start |
| `EPUBLIBRE_TIMEOUT` | `30` | HTTP timeout in seconds |
| `EPUBLIBRE_RETRIES` | `2` | Retries after the initial request |
| `EPUBLIBRE_REQUEST_DELAY` | `7` | Seconds between requests |
| `EPUBLIBRE_BLOCK_DELAY` | `300` | Pause before retrying a failed catalogue request |
| `EPUBLIBRE_OUT_DIR` | `public` | Generated output directory |
| `EPUBLIBRE_SITE_BASE_URL` | unset | Deployed site root used for RSS self-links |

## Caveats

- The project is unofficial and unaffiliated with Pawchive or Patreon.
- Feed availability depends on Patreon's web API and Pawchive's archive contents.
- The Patreon session cookie expires when the account signs out or Patreon
  revokes it; the build then falls back to cum.st/Pawchive and logs a warning.
- Posts are published only when their titles match the feed's pattern; a
  chapter whose title breaks the pattern is dropped.
- The Royal Road feed depends on its syndication and chapter-page structures;
  it is unofficial and unaffiliated with Royal Road or its author.
