# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this repo is

Generates full-text RSS 2.0 feeds for web fiction sources (Pawchive/Patreon archives, Royal Road) and deploys them to GitHub Pages. It follows the standalone-builder structure of `pib_feed`: one top-level Python script per source, each writing to `public/<feed-key>/feed.xml` plus a companion `index.html`. `public/` is gitignored generated output.

## Commands

```bash
python3 -m pip install -r requirements.txt

# Build feeds (hits live network APIs)
python3 pawchive_feed.py                    # full build incl. Patreon/cum.st sync
python3 pawchive_feed.py --skip-patreon-sync  # build only, no external sync checks
python3 royalroad_feed.py

# Pawchive CLI utilities (see DOCS.md for details)
python3 pawchive_feed.py --check-flag <post-id-or-url>
python3 pawchive_feed.py --flag <post-id-or-url>
python3 pawchive_feed.py --check-patreon
python3 pawchive_feed.py --check-cumst
python3 pawchive_feed.py --session <cookie>  # override Pawchive session cookie

# Tests (unittest-style classes, run via pytest)
python3 -m pytest                                  # all tests
python3 -m pytest test_pawchive_feed.py            # one file
python3 -m pytest test_pawchive_feed.py::PawchiveFeedTests::test_feed_defs_unique  # one test
```

Tests are offline: they mock HTTP and use `tempfile` output dirs.

## Architecture

**`rss_common.py`** holds all source-agnostic plumbing: session construction (`make_session`), retrying fetches (`fetch`/`fetch_json`), URL scheme sanitizing (`safe_url`), date coercion, RSS item/document rendering (`render_rss_item`, `build_feed_document`), file writing (`write_feed_files`), history loading (`load_feed_documents`), the merge/eviction engine (`merge_new_items`), and output pruning (`directory_has_signature`, `prune_untracked_feeds`). Builders (`pawchive_feed.py`, `royalroad_feed.py`) should stay thin source-specific adapters — put shared logic in `rss_common.py`, not duplicated across builders.

**The merge/eviction invariant** (implemented in `merge_new_items`, used by both builders): load previously published RSS first (local `public/<key>/feed.xml` plus, when `*_SITE_BASE_URL` is set, the deployed copy — this is how history survives fresh CI checkouts), then only fetch/render candidates that are absent by identity key AND unseen GUID AND strictly newer than the newest retained item. The item cap (`*_MAX_ITEMS`, default 10) is applied after merging, newest-first, so new items only ever displace the oldest retained ones. Don't reorder these steps or fetch bodies before merging.

**Per-source pruning:** both builders share `public/`, so each prunes only directories matching its own content signature (`is_pawchive_feed_dir` / `is_royalroad_feed_dir` via `directory_has_signature`, with markers and excludes). When adding a new builder, give it its own signature and exclude the other sources' markers so builders never delete each other's output.

**Feed configuration** lives in the `FEEDS` list at the top of each builder (pawchive keys: `key`, `creator_id`, `campaign_id`, `fallback_name`, `title`, `author`; royalroad keys: `key`, `fiction_id`, `slug`, `title`, `author`). `royalroad_feed.FEEDS` is currently empty. Runtime settings come from `PAWCHIVE_*` / `ROYALROAD_*` env vars (see tables in DOCS.md).

**Chapter sanitization is source-specific by design:** Pawchive uses a stdlib `HTMLParser` subclass (`ChapterSanitizer`) with tag/attribute allowlists that escapes unknown tags to text; Royal Road uses BeautifulSoup to extract `div.chapter-inner` and strip CSS-hidden watermark classes discovered from page-local `<style>` blocks. Both rewrite `href`/`src` to absolute, safe-scheme URLs via `rss.safe_url`. Extraction behavior intentionally mirrors WebToEpub's `PawchiveParser`/`RoyalRoadParser`.

**Patreon sync (pawchive only):** after building, `run_feed` checks Patreon's and cum.st's public APIs for posts newer than the feed baseline; posts missing from Pawchive get a polite check-before-set re-import flag (`flag_post_politely` checks flag status via GET before POSTing). The `PAWCHIVE_SESSION` cookie (hardcoded default, env/`--session` override) authenticates flag requests.

## Coupled artifacts — update together

Changing a builder's `FEEDS` list requires updating, in the same change:
- `OPML/*.opml` (importable feed collections)
- the tests that pin feed config: `test_feed_defs_unique` in `test_pawchive_feed.py` / `test_royalroad_feed.py` (assert exact feed counts) and `test_opml.py` (asserts URLs point at `https://jumpingpony.github.io/misc_feed/`, and that `royalroad.opml` has zero outlines while `FEEDS` is empty)
- `DOCS.md` feed tables

## CI

`.github/workflows/build-feeds.yml` runs on a daily schedule, on `workflow_dispatch`, and on pushes to `main` touching the builders/requirements/workflow; it builds both feeds with `*_SITE_BASE_URL` set to the Pages URL and deploys `public/` to GitHub Pages. A separate keepalive workflow commits if the repo is idle 45+ days.
