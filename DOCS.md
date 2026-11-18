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
| voidherald/hr | The Hundred Reigns | Patreon `16493499` (Void Herald) | [feed.xml](https://jumpingpony.github.io/misc_feed/voidherald/hr/feed.xml) |
| mistervii/all | Collected Serials | Patreon `64285829` (MisterVii) | [feed.xml](https://jumpingpony.github.io/misc_feed/mistervii/all/feed.xml) |

Chapter bodies come from cum.st first, then Pawchive: cum.st usually carries a
new post before Pawchive's archive ingests it, and Pawchive fills in posts
cum.st lacks. A slower archive never replaces a cum.st body. Chapter extraction
uses the same HTML and unknown-tag handling as WebToEpub's `PawchiveParser`,
then removes previous/next navigation and attachments. Author notes, their
links, and separators remain part of the chapter. The remaining chapter HTML is
stored in each item's `<content:encoded>` element.

Each feed retains the 30 newest chapter posts. A per-feed title pattern decides
what counts as a chapter:

- `cerim/hdt` keeps titles containing "chapter", so polls, art posts, and
  announcements are dropped.
- `voidherald/hr` keeps numbered titles like `The Hundred Reigns 286: ...`, so
  HR art galleries and polls are dropped even though they carry the series name.
- `mistervii/all` keeps titles containing "chapter" across The Strongest
  Spellblade, Foxy Blight, and The Systemic Lands; one feed covers all three
  series.

Pawchive enforces 50-result API pages, so the builder requests only the first
page for each creator. It loads the published RSS first, renders only absent
posts whose publication time is newer than the newest retained item, merges them
by canonical Patreon post ID, and trims the oldest items only after merging. It
does not crawl listing HTML or fetch individual post pages.

### Post arrival schedule

For `cerim/hdt` (Patreon `31891971`), measured over 49 chapters from August to
November 2026:

- **Active Schedule**: 5 days a week (**Sunday through Thursday**).
- **Publication Times**: **10:23 – 10:59 Europe/Berlin**, with occasional
  earlier releases (09:13 was the earliest seen). That is 08:23 – 08:59 UTC
  during CEST and 09:23 – 09:59 UTC during CET.

The hourly builds pick a chapter up at the next `:25` slot, so a release goes
out within an hour of publication.

### Patreon post check

During each build run, `pawchive_feed.py` also checks Patreon's public API for
newly published posts. If a newer post exists on Patreon that Pawchive has not
ingested yet, it performs a **polite check-before-set** re-import request:
1. Queries `GET /api/v1/patreon/user/{creator_id}/post/{post_id}/flag`.
2. If already flagged, it skips politely without making write requests.
3. If unflagged, it sends `POST /api/v1/patreon/user/{creator_id}/post/{post_id}/flag` using the Pawchive session cookie.

The check reads only public post metadata, so it needs no Patreon account.

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

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PAWCHIVE_TIMEOUT` | `30` | HTTP timeout in seconds |
| `PAWCHIVE_RETRIES` | `2` | Retries after the initial request |
| `PAWCHIVE_MAX_ITEMS` | `30` | Maximum retained items per feed |
| `PAWCHIVE_OUT_DIR` | `public` | Generated output directory |
| `PAWCHIVE_SITE_BASE_URL` | unset | Deployed site root used for RSS self-links |
| `PAWCHIVE_SESSION` | hardcoded session | Pawchive login session cookie used for authenticated flag requests |

The GitHub Actions workflow builds every hour at :25 UTC, on pushes to `main`
that touch the builders, and on demand, then deploys `public/` to GitHub Pages.
Importable feed collections are available in `OPML/`.

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
- Feed availability depends on cum.st and Pawchive's archive contents.
- New-post detection reads Patreon's public post list, which needs no account.
- Posts are published only when their titles match the feed's pattern; a
  chapter whose title breaks the pattern is dropped.
