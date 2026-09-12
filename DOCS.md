# Miscellaneous full-text RSS feeds

This repository follows the standalone-builder structure of `pib_feed`: each
top-level Python script owns one source and writes its generated files below
`public/<feed-key>/`.

## Pawchive Patreon feeds

`pawchive_feed.py` publishes full-text RSS 2.0 feeds for selected Patreon creators
archived by Pawchive:

| Feed | Novel | Creator / Author | GitHub Pages |
|------|-------|------------------|--------------|
| cerim | Hell Difficulty Tutorial | Patreon `31891971` (Cerim) | [feed.xml](https://jumpingpony.github.io/misc_feed/cerim/feed.xml) |
| scyzim | Nuclear Son | Patreon `76415047` (Scyzim) | [feed.xml](https://jumpingpony.github.io/misc_feed/scyzim/feed.xml) |
| void-herald | The Hundred Reigns | Patreon `16493499` (Void Herald) | [feed.xml](https://jumpingpony.github.io/misc_feed/void-herald/feed.xml) |
| laughyeamer | Godless Sword | Patreon `143885136` (LaughYeAmer) | [feed.xml](https://jumpingpony.github.io/misc_feed/laughyeamer/feed.xml) |

Post discovery and complete post objects come from the
[Pawchive v1 API](https://pawchive.pw/api/schema). Chapter extraction uses the
same post-content field and unknown-tag handling as WebToEpub's
`PawchiveParser`, then removes Patreon previous/next navigation and attachments.
Author notes, their links, and separators remain part of the chapter. The
remaining chapter HTML is stored in each item's `<content:encoded>` element.

Each feed retains the 10 newest posts. Pawchive enforces 50-result API pages, so
the builder requests only the first page for each creator. It loads the published
RSS first, renders only absent posts whose publication time is newer than the
newest retained item, merges them by permalink, and trims the oldest items only
after merging. It does not crawl listing HTML or fetch individual post pages.

### Post Arrival Schedule & Patreon Sync

For `cerim` (Patreon `31891971`):
- **Active Schedule**: 5 days a week (**Sunday through Thursday**).
- **Publication Times**: Typically between **08:00 – 09:30 UTC** (**13:30 – 15:00 IST**), averaging around **08:35 UTC / 14:05 IST**.

During each build run (scheduled every 4 hours), `pawchive_feed.py` automatically checks Patreon's public API for newly published posts. If a newer post exists on Patreon that has not yet been ingested by Pawchive, it performs a **polite check-before-set** re-import request:
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

# Check Patreon for newer posts and politely flag any missing ones
python3 pawchive_feed.py --check-patreon

# Explicitly check-and-flag a specific post or URL
python3 pawchive_feed.py --flag 167183185

# Build feeds without checking Patreon
python3 pawchive_feed.py --skip-patreon-sync
```

Output:

- `public/cerim/feed.xml`
- `public/scyzim/feed.xml`
- `public/void-herald/feed.xml`
- `public/laughyeamer/feed.xml`

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PAWCHIVE_TIMEOUT` | `30` | HTTP timeout in seconds |
| `PAWCHIVE_RETRIES` | `2` | Retries after the initial request |
| `PAWCHIVE_MAX_ITEMS` | `10` | Maximum retained items per feed |
| `PAWCHIVE_OUT_DIR` | `public` | Generated output directory |
| `PAWCHIVE_SITE_BASE_URL` | unset | Deployed site root used for RSS self-links |
| `PAWCHIVE_SESSION` | hardcoded session | Pawchive login session cookie used for authenticated flag requests |

The GitHub Actions workflow runs every 4 hours and deploys `public/` to GitHub
Pages. Importable feed collections are available in `OPML/`.

## Royal Road fiction feeds

`royalroad_feed.py` publishes the 10 newest full chapters for one Royal Road
story:

| Feed | Novel | Author | GitHub Pages |
|------|-------|--------|--------------|
| zenith-of-sorcery | Zenith of Sorcery | nobody103 | [feed.xml](https://jumpingpony.github.io/misc_feed/zenith-of-sorcery/feed.xml) |

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

Run locally with `python3 royalroad_feed.py`; output is written to
`public/zenith-of-sorcery/feed.xml`.

Royal Road configuration:

| Variable | Default | Meaning |
|----------|---------|---------|
| `ROYALROAD_TIMEOUT` | `30` | HTTP timeout in seconds |
| `ROYALROAD_RETRIES` | `2` | Retries after the initial request |
| `ROYALROAD_MAX_ITEMS` | `10` | Maximum retained items per feed |
| `ROYALROAD_REQUEST_DELAY` | `1` | Delay between newly fetched chapter pages |
| `ROYALROAD_OUT_DIR` | `public` | Generated output directory |
| `ROYALROAD_SITE_BASE_URL` | unset | Deployed site root used for self-links and body reuse |

## Caveats

- The project is unofficial and unaffiliated with Pawchive or Patreon.
- Feed availability depends on Pawchive's API and archive contents.
- Some posts that are announcements rather than chapters may still contain
  non-story prose because there is no universal semantic marker for it.
- The Royal Road feed depends on its syndication and chapter-page structures;
  it is unofficial and unaffiliated with Royal Road or its author.
