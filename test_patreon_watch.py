from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import Mock, patch

import patreon_watch as watch
from pawchive_feed import FEEDS

UTC = dt.timezone.utc


class PatreonWatchTests(unittest.TestCase):
    # Unit tests for the release-window watcher.

    def test_berlin_offset_follows_eu_dst(self):
        # EU switches at 01:00 UTC on the last Sunday of March and October.
        self.assertEqual(1, watch.berlin_offset(dt.datetime(2026, 3, 29, 0, 59, tzinfo=UTC)))
        self.assertEqual(2, watch.berlin_offset(dt.datetime(2026, 3, 29, 1, 0, tzinfo=UTC)))
        self.assertEqual(2, watch.berlin_offset(dt.datetime(2026, 10, 25, 0, 59, tzinfo=UTC)))
        self.assertEqual(1, watch.berlin_offset(dt.datetime(2026, 10, 25, 1, 0, tzinfo=UTC)))

    def test_release_window_summer(self):
        # 08:50-11:05 CEST is 06:50-09:05 UTC.
        start, end = watch.release_window(dt.datetime(2026, 7, 15, 0, 0, tzinfo=UTC))
        self.assertEqual(dt.datetime(2026, 7, 15, 6, 50, tzinfo=UTC), start)
        self.assertEqual(dt.datetime(2026, 7, 15, 9, 5, tzinfo=UTC), end)

    def test_release_window_winter(self):
        # 08:50-11:05 CET is 07:50-10:05 UTC.
        start, end = watch.release_window(dt.datetime(2026, 1, 15, 0, 0, tzinfo=UTC))
        self.assertEqual(dt.datetime(2026, 1, 15, 7, 50, tzinfo=UTC), start)
        self.assertEqual(dt.datetime(2026, 1, 15, 10, 5, tzinfo=UTC), end)

    def test_has_new_chapter_when_id_missing(self):
        # A chapter absent from the published feed triggers a build.
        chapter = {
            "id": "957",
            "title": "Chapter 957 - The start",
            "published": dt.datetime(2026, 11, 2, 9, 57, tzinfo=UTC),
        }
        with patch.object(watch, "newest_chapter", return_value=chapter), patch.object(
            watch, "published_post_ids", return_value={"956"}
        ):
            self.assertTrue(watch.has_new_chapter(Mock(), FEEDS[0]))

    def test_has_new_chapter_when_already_published(self):
        # An already published chapter does not trigger another build.
        chapter = {
            "id": "956",
            "title": "Chapter 956 - Test subject 99",
            "published": dt.datetime(2026, 11, 1, 9, 56, tzinfo=UTC),
        }
        with patch.object(watch, "newest_chapter", return_value=chapter), patch.object(
            watch, "published_post_ids", return_value={"956"}
        ):
            self.assertFalse(watch.has_new_chapter(Mock(), FEEDS[0]))

    def test_has_new_chapter_without_published_feed(self):
        # A missing feed cannot prove the chapter is old, so build once.
        chapter = {
            "id": "956",
            "title": "Chapter 956 - Test subject 99",
            "published": dt.datetime(2026, 11, 1, 9, 56, tzinfo=UTC),
        }
        with patch.object(watch, "newest_chapter", return_value=chapter), patch.object(
            watch, "published_post_ids", return_value=None
        ):
            self.assertTrue(watch.has_new_chapter(Mock(), FEEDS[0]))


if __name__ == "__main__":
    unittest.main()
