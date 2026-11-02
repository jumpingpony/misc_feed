#!/usr/bin/env python3
"""Wait for the daily Patreon release window and report a new chapter.

GitHub's scheduler can start a cron run 5-30 minutes late, so the workflow
cannot fire at 10:30 Europe/Berlin and expect to be on time.  This watcher
starts early, sleeps until the window opens, then polls post metadata until a
chapter that the published feed does not contain yet appears.

It only reads public metadata and the published feed; the Patreon cookie is
not needed here and is never passed to this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time

import rss_common as rss
from pawchive_feed import (
    FEEDS,
    OUT_DIR,
    RETRIES,
    SITE_BASE_URL,
    TIMEOUT,
    extract_post_id,
    get_latest_patreon_posts,
    matches_title_filter,
    parse_existing_items,
    title_filter_for,
)

UTC = dt.timezone.utc

# Cerim publishes at 10:23-10:59 Europe/Berlin, Sunday through Thursday.
WINDOW_START = dt.time(8, 50)
WINDOW_END = dt.time(11, 5)
POLL_INTERVAL = 240
RECENT_POSTS = 5
DEFAULT_RELEASE_DAYS = "0,1,2,3,6"  # Monday-Thursday and Sunday


def last_sunday(year: int, month: int) -> dt.date:
    """Return the last Sunday of a month, which is when EU DST flips."""
    if month == 12:
        last_day = dt.date(year, 12, 31)
    else:
        last_day = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return last_day - dt.timedelta(days=(last_day.weekday() + 1) % 7)


def berlin_offset(moment: dt.datetime) -> int:
    """Return +1 (CET) or +2 (CEST) for a UTC instant."""
    year = moment.year
    summer_start = dt.datetime.combine(last_sunday(year, 3), dt.time(1), UTC)
    summer_end = dt.datetime.combine(last_sunday(year, 10), dt.time(1), UTC)
    return 2 if summer_start <= moment < summer_end else 1


def release_window(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Return today's release window as UTC instants."""
    offset = dt.timedelta(hours=berlin_offset(now))
    local_date = (now + offset).date()
    start = dt.datetime.combine(local_date, WINDOW_START, UTC) - offset
    end = dt.datetime.combine(local_date, WINDOW_END, UTC) - offset
    return start, end


def published_posts(session, feed: dict) -> dict[str, dt.datetime] | None:
    """Map post IDs already present in the local or deployed feed to their dates."""
    documents = rss.load_feed_documents(
        session,
        out_dir=OUT_DIR,
        key=feed["key"],
        site_base_url=SITE_BASE_URL,
        timeout=TIMEOUT,
        retries=RETRIES,
    )
    if not documents:
        return None

    pattern = title_filter_for(feed)
    posts: dict[str, dt.datetime] = {}

    # Identities are canonical Patreon permalinks; only the numeric ID matters.
    for document in documents:
        parsed = parse_existing_items(document.decode("utf-8-sig"), pattern)
        for identity, (published, _) in parsed.items():
            post_id = extract_post_id(identity)
            if post_id:
                posts[post_id] = published

    return posts


def parse_release_days(raw: str) -> set[int]:
    """Parse a comma-separated weekday list (Monday=0) into a set."""
    days: set[int] = set()

    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            day = int(chunk)
        except ValueError:
            continue
        if 0 <= day <= 6:
            days.add(day)

    return days or {0, 1, 2, 3, 6}


def watched_feeds() -> list[dict]:
    """Feeds whose release window this watcher monitors."""
    return [feed for feed in FEEDS if feed.get("watch", False)]


def newest_chapter(session, feed: dict) -> dict | None:
    """Return the newest published post whose title matches the feed pattern."""
    campaign_id = feed.get("campaign_id")
    if not campaign_id:
        return None

    posts = get_latest_patreon_posts(session, campaign_id, count=RECENT_POSTS)
    pattern = title_filter_for(feed)
    chapters = [
        post for post in posts if matches_title_filter(post["title"], pattern)
    ]
    if not chapters:
        return None

    return max(chapters, key=lambda post: post["published"])


def has_new_chapter(session, feed: dict) -> bool:
    """True when Patreon lists a chapter the published feed lacks."""
    chapter = newest_chapter(session, feed)
    if chapter is None:
        print(f"  [watch] {feed['key']}: no chapter post found")
        return False

    known = published_posts(session, feed)
    if known is None:
        print(f"  [watch] {feed['key']}: published feed unavailable; building once")
        return True

    if chapter["id"] in known:
        print(f"  [watch] {feed['key']}: newest chapter {chapter['id']} already published")
        return False

    # One scheduled chapter per day: stop as soon as today's is in the feed.
    now = dt.datetime.now(UTC)
    offset = dt.timedelta(hours=berlin_offset(now))
    today = (now + offset).date()
    if any((published + offset).date() == today for published in known.values()):
        print(f"  [watch] {feed['key']}: today's chapter is already uploaded; stopping")
        return False

    print(
        f"  [watch] {feed['key']}: new chapter {chapter['id']} "
        f"({chapter['title']}) at {chapter['published']:%H:%M} UTC"
    )
    return True


def set_output(name: str, value: str) -> None:
    """Write a GitHub Actions step output when running inside a workflow."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"[watch] {name}={value}")
        return

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    """Wait for the window, poll Patreon, and report whether to build."""
    parser = argparse.ArgumentParser(
        description="Poll Patreon during the daily release window.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once instead of waiting for and polling the window",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("PATREON_WATCH_INTERVAL", POLL_INTERVAL)),
        help="Seconds between polls inside the window",
    )
    opts = parser.parse_args(argv)

    now = dt.datetime.now(UTC)
    offset = dt.timedelta(hours=berlin_offset(now))
    local_now = now + offset

    # Cerim publishes Sunday through Thursday; skip Friday and Saturday.
    release_days = parse_release_days(
        os.environ.get("PATREON_WATCH_DAYS", DEFAULT_RELEASE_DAYS)
    )
    if not opts.once and local_now.weekday() not in release_days:
        print(f"[watch] {local_now:%A} is outside the release schedule; stopping")
        set_output("found", "false")
        return 0

    start, end = release_window(now)
    print(f"[watch] window {start:%Y-%m-%d %H:%M} - {end:%H:%M} UTC")

    # Start early enough that scheduler drift still lands inside the window.
    if not opts.once and now < start:
        wait = (start - now).total_seconds()
        print(f"[watch] waiting {int(wait // 60)} min for the window to open")
        time.sleep(wait)

    session = rss.make_session(accept=rss.AcceptFormat.JSON)

    while True:
        for feed in watched_feeds():
            if has_new_chapter(session, feed):
                print("[watch] chapter found; stopping polls")
                set_output("found", "true")
                return 0

        remaining = (end - dt.datetime.now(UTC)).total_seconds()
        if opts.once or remaining <= 0:
            break
        time.sleep(min(opts.interval, remaining))

    print("[watch] window closed with no new chapter")
    set_output("found", "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
