from __future__ import annotations

import datetime as dt
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import pawchive_feed as feed


class PawchiveFeedTests(unittest.TestCase):
    # Unit tests for Pawchive feed builder and Patreon sync.

    @staticmethod
    def make_post(post_id, day):
        # Construct synthetic Pawchive post dictionary for testing.
        return {
            "id": str(post_id),
            "user": "34",
            "service": "patreon",
            "title": f"Chapter {post_id}",
            "content": f"<p>Body {post_id}</p>",
            "published": f"2026-08-{day:02d}T10:00:00",
            "attachments": [],
        }

    def test_feed_defs_unique(self):
        # Verify feed configurations contain unique keys and creator IDs.
        self.assertEqual(2, len(feed.FEEDS))
        self.assertEqual(2, len({item["key"] for item in feed.FEEDS}))
        self.assertEqual(2, len({item["creator_id"] for item in feed.FEEDS}))

    def test_build_chapter_body_only(self):
        # Verify chapter builder sanitizes HTML, strips navigation, and keeps author notes.
        post = {
            "id": "12",
            "user": "34",
            "service": "patreon",
            "content": (
                '<blockquote><p><a href="https://example.com/previous">'
                'Previous Chapter</a></p></blockquote>'
                '<p>Hello <em>world</em>. <a href="/map">Map</a></p>'
                '<script>bad()</script><foo>x</foo>'
                '<p>***</p><p><em>Author Note: buy the book.</em></p>'
                '<p><a href="https://example.com/next">Next chapter</a></p>'
            ),
            "attachments": [
                {"name": "cover.JPG", "path": "/aa/cover.jpg"},
                {"name": "chapter.epub", "path": "bb/chapter.epub"},
            ],
        }

        # Build chapter HTML.
        body = feed.build_chapter(post)

        # Assert allowed tags preserved and forbidden tags removed.
        self.assertIn("<p>Hello <em>world</em>.", body)
        self.assertNotIn("bad()", body)
        self.assertIn("&lt;foo&gt;x&lt;/foo&gt;", body)
        self.assertIn('href="https://pawchive.pw/map"', body)
        self.assertNotIn("Previous Chapter", body)
        self.assertNotIn("Next chapter", body)
        self.assertIn("Author Note: buy the book.", body)
        self.assertIn("***", body)
        self.assertNotIn("Attachments", body)
        self.assertNotIn("cover.jpg", body)
        self.assertNotIn("chapter.epub", body)

    def test_feed_contains_full_body(self):
        # Verify XML feed includes complete chapter HTML in content:encoded.
        post = {
            "id": "12",
            "user": "34",
            "service": "patreon",
            "title": "Chapter 12",
            "content": "<p>The complete chapter.</p>",
            "published": "2026-08-01T10:30:00",
            "attachments": [],
        }

        # Render item and assemble XML document.
        rendered = feed.render_item(post)
        items = {
            feed.post_permalink(post): (
                dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                rendered,
            )
        }
        xml = feed.build_feed_xml(feed.FEEDS[0], "Author", items)

        # Parse XML and inspect content:encoded element.
        root = ET.fromstring(xml)
        encoded = root.find(
            "./channel/item/{http://purl.org/rss/1.0/modules/content/}encoded"
        )
        self.assertIsNotNone(encoded)
        self.assertEqual("<p>The complete chapter.</p>", encoded.text)

    def test_noop_merge_renders_zero(self):
        # Verify merging existing posts triggers zero new renders.
        posts = [self.make_post(i, i) for i in range(1, 4)]
        existing = {
            feed.post_permalink(post): (
                feed.parse_date(post["published"]),
                feed.render_item(post),
            )
            for post in posts
        }
        renderer = Mock(side_effect=feed.render_item)

        # Merge identical posts with mocked renderer.
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, rendered, evicted = feed.merge_new_posts(
                existing, list(reversed(posts)), renderer
            )

        self.assertEqual(existing, merged)
        self.assertEqual((0, 0), (rendered, evicted))
        renderer.assert_not_called()

    def test_merge_evicts_oldest(self):
        # Verify newest post is added and oldest post is evicted when exceeding limit.
        old_posts = [self.make_post(i, i) for i in range(1, 4)]
        new_post = self.make_post(4, 4)
        stale_post = self.make_post(99, 1)
        existing = {
            feed.post_permalink(post): (
                feed.parse_date(post["published"]),
                feed.render_item(post),
            )
            for post in old_posts
        }
        renderer = Mock(side_effect=feed.render_item)
        listing = [new_post, new_post, old_posts[-1], stale_post]

        # Merge with 3-item limit.
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, rendered, evicted = feed.merge_new_posts(
                existing, listing, renderer
            )

        self.assertEqual((1, 1), (rendered, evicted))
        self.assertEqual(
            {feed.post_permalink(post) for post in (old_posts[1], old_posts[2], new_post)},
            set(merged),
        )
        renderer.assert_called_once_with(new_post)

    def test_existing_xml_roundtrip(self):
        # Verify existing XML item block can be parsed without loss of content.
        post = self.make_post(7, 7)
        identity = feed.post_permalink(post)
        original = feed.render_item(post)
        document = f"<rss><channel>{original}</channel></rss>"

        # Parse items from document.
        parsed = feed.parse_existing_items(document)
        self.assertEqual([identity], list(parsed))
        self.assertEqual(original.strip(), parsed[identity][1])

    def test_parse_post_identifier(self):
        # Verify identifier parser handles raw IDs, Patreon URLs, and Pawchive URLs.
        # Plain ID
        self.assertEqual(
            ("patreon", "31891971", "167183185"),
            feed.parse_post_identifier("167183185"),
        )
        # Patreon simple URL
        self.assertEqual(
            ("patreon", "31891971", "167183185"),
            feed.parse_post_identifier("https://www.patreon.com/posts/167183185"),
        )
        # Patreon creator slug URL
        self.assertEqual(
            ("patreon", "31891971", "167183185"),
            feed.parse_post_identifier(
                "https://www.patreon.com/cerim/posts/chapter-910-four-167183185"
            ),
        )
        # Pawchive URL
        self.assertEqual(
            ("patreon", "31891971", "167183185"),
            feed.parse_post_identifier(
                "https://pawchive.pw/patreon/user/31891971/post/167183185"
            ),
        )
        with self.assertRaises(ValueError):
            feed.parse_post_identifier("invalid_url_or_id")

    def test_check_post_flag(self):
        # Verify flag status endpoint returns True on 200 and False on 404.
        session = Mock()
        session.get.return_value = Mock(status_code=HTTPStatus.OK)
        self.assertTrue(feed.check_post_flag(session, "patreon", "31891971", "123"))

        session.get.return_value = Mock(status_code=HTTPStatus.NOT_FOUND)
        self.assertFalse(feed.check_post_flag(session, "patreon", "31891971", "123"))

    def test_flag_skips_if_flagged(self):
        # Verify polite flagger skips POST when post is already flagged.
        session = Mock()
        # GET returns 200 (already flagged)
        session.get.return_value = Mock(status_code=HTTPStatus.OK)
        ok, reason = feed.flag_post_politely(session, "patreon", "31891971", "123")
        self.assertTrue(ok)
        self.assertEqual("already flagged", reason)
        session.post.assert_not_called()

    def test_flag_sets_when_unset(self):
        # Verify polite flagger sends POST when post is not yet flagged.
        session = Mock()
        # GET returns 404 (unset), POST returns 201 (success)
        session.get.return_value = Mock(status_code=HTTPStatus.NOT_FOUND)
        session.post.return_value = Mock(status_code=HTTPStatus.CREATED)
        ok, reason = feed.flag_post_politely(session, "patreon", "31891971", "123")
        self.assertTrue(ok)
        self.assertEqual("newly flagged", reason)
        session.post.assert_called_once()

    def test_flag_handles_statuses(self):
        # Verify polite flagger maps HTTP conflict and unauthorized statuses.
        session = Mock()
        session.get.return_value = Mock(status_code=HTTPStatus.NOT_FOUND)
        session.post.return_value = Mock(status_code=HTTPStatus.CONFLICT)
        ok, reason = feed.flag_post_politely(session, "patreon", "31891971", "123")
        self.assertTrue(ok)
        self.assertIn("already flagged", reason)

        session.post.return_value = Mock(status_code=HTTPStatus.UNAUTHORIZED)
        ok, reason = feed.flag_post_politely(session, "patreon", "31891971", "123")
        self.assertFalse(ok)
        self.assertIn("unauthorized", reason)

    def test_get_latest_patreon_posts(self):
        # Verify parsing of post payloads from Patreon campaign API.
        session = Mock()
        session.get.return_value = Mock(
            status_code=HTTPStatus.OK,
            json=lambda: {
                "data": [
                    {
                        "id": "167183185",
                        "attributes": {
                            "title": "Chapter 910",
                            "published_at": "2026-08-20T08:44:47.000+00:00",
                            "url": "https://www.patreon.com/posts/167183185",
                        },
                    }
                ]
            },
        )
        posts = feed.get_latest_patreon_posts(session, "10143762")
        self.assertEqual(1, len(posts))
        self.assertEqual("167183185", posts[0]["id"])
        self.assertEqual("Chapter 910", posts[0]["title"])
        self.assertEqual(
            dt.datetime(2026, 8, 20, 8, 44, 47, tzinfo=dt.timezone.utc),
            posts[0]["published"],
        )

    def test_sync_flags_newer_only(self):
        # Verify sync only flags posts published after newest retained date.
        session = Mock()
        session.get.return_value = Mock(
            status_code=HTTPStatus.OK,
            json=lambda: {
                "data": [
                    {
                        "id": "200",
                        "attributes": {
                            "title": "New Ch",
                            "published_at": "2026-08-20T10:00:00.000+00:00",
                        },
                    },
                    {
                        "id": "100",
                        "attributes": {
                            "title": "Old Ch",
                            "published_at": "2026-08-19T10:00:00.000+00:00",
                        },
                    },
                ]
            },
        )
        feed_config = {
            "key": "cerim",
            "creator_id": "31891971",
            "campaign_id": "10143762",
        }
        newest_pawchive_date = dt.datetime(
            2026, 8, 19, 12, 0, 0, tzinfo=dt.timezone.utc
        )

        # Execute Patreon sync.
        with patch(
            "pawchive_feed.flag_post_politely", return_value=(True, "newly flagged")
        ) as mock_flag:
            flagged = feed.sync_and_flag_patreon_posts(
                session, feed_config, newest_pawchive_date
            )
            self.assertEqual(1, len(flagged))
            self.assertEqual("200", flagged[0]["id"])
            mock_flag.assert_called_once_with(
                session, "patreon", "31891971", "200"
            )

    def test_sync_skips_imported(self):
        # Verify sync skips posts present in known imported IDs list.
        session = Mock()
        session.get.return_value = Mock(
            status_code=HTTPStatus.OK,
            json=lambda: {
                "data": [
                    {
                        "id": "200",
                        "attributes": {
                            "title": "Already Imported Ch",
                            "published_at": "2026-08-20T10:00:00.000+00:00",
                        },
                    },
                ]
            },
        )
        feed_config = {
            "key": "cerim",
            "creator_id": "31891971",
            "campaign_id": "10143762",
        }
        newest_pawchive_date = dt.datetime(
            2026, 8, 19, 12, 0, 0, tzinfo=dt.timezone.utc
        )
        known_imported_ids = {"200"}

        # Execute Patreon sync with imported post ID.
        with patch("pawchive_feed.flag_post_politely") as mock_flag:
            flagged = feed.sync_and_flag_patreon_posts(
                session,
                feed_config,
                newest_pawchive_date,
                known_imported_ids=known_imported_ids,
            )
            self.assertEqual(0, len(flagged))
            mock_flag.assert_not_called()

    def test_prune_untracked_dirs(self):
        # Verify untracked Pawchive directories are pruned while retaining others.
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            active_dir = out_dir / "cerim"
            active_dir.mkdir()
            (active_dir / "feed.xml").write_text(
                "<rss><channel><title>cerim — Pawchive</title>"
                "<link>https://pawchive.pw</link></channel></rss>"
            )

            untracked_pawchive = out_dir / "old-feed"
            untracked_pawchive.mkdir()
            (untracked_pawchive / "feed.xml").write_text(
                "<rss><channel><title>old — Pawchive</title>"
                "<link>https://pawchive.pw</link></channel></rss>"
            )

            other_dir = out_dir / "zenith-of-sorcery"
            other_dir.mkdir()
            (other_dir / "feed.xml").write_text(
                "<rss><channel><title>Zenith — Royal Road</title>"
                "<link>https://www.royalroad.com</link></channel></rss>"
            )

            # Prune directories not in active keys.
            pruned = feed.prune_untracked_feeds(out_dir, {"cerim"})
            self.assertEqual(["old-feed"], pruned)
            self.assertTrue(active_dir.exists())
            self.assertFalse(untracked_pawchive.exists())
            self.assertTrue(other_dir.exists())


if __name__ == "__main__":
    unittest.main()
