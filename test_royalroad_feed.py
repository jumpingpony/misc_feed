from __future__ import annotations

import datetime as dt
from enum import Enum, auto
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import royalroad_feed as feed


class BodyOption(Enum):
    # Option to include or omit chapter body in test fixture generation.

    INCLUDE = auto()
    OMIT = auto()


class RoyalRoadFeedTests(unittest.TestCase):
    # Unit tests for Royal Road feed builder and chapter extractor.

    def test_feed_defs_unique(self):
        # Verify feed configurations contain unique keys and fiction IDs.
        self.assertEqual(0, len(feed.FEEDS))
        self.assertEqual(0, len({item["key"] for item in feed.FEEDS}))
        self.assertEqual(0, len({item["fiction_id"] for item in feed.FEEDS}))

    @staticmethod
    def make_chapter(
        chapter_id,
        day,
        *,
        body: BodyOption = BodyOption.INCLUDE,
        link_id=None,
    ):
        # Construct synthetic chapter metadata dictionary for tests.
        chapter = {
            "title": f"Chapter {chapter_id}",
            "link": f"https://www.royalroad.com/fiction/chapter/{link_id or chapter_id}",
            "guid": str(chapter_id),
            "date": dt.datetime(2026, 8, day, tzinfo=dt.timezone.utc),
        }
        if body == BodyOption.INCLUDE:
            chapter["body_html"] = f"<p>Body {chapter_id}</p>"
        return chapter

    def test_feed_title_format(self):
        # Verify feed title follows 'novel title - Author' format.
        test_feed = {"title": "Zenith of Sorcery", "author": "nobody103"}
        self.assertEqual("Zenith of Sorcery - nobody103", feed.feed_title(test_feed))

    def test_parse_syndication_limit(self):
        # Verify syndication parser limits output to maximum items and cleans title prefix.
        items = "".join(
            f"<item><title>Zenith of Sorcery - Chapter {i}</title>"
            f"<link>https://www.royalroad.com/fiction/chapter/{i}</link>"
            f"<guid>{i}</guid><pubDate>Tue, 04 Aug 2026 21:00:16 GMT</pubDate></item>"
            for i in range(12)
        )
        document = f"<rss><channel>{items}</channel></rss>".encode()

        # Parse syndication feed XML.
        feed_config = {"title": "Zenith of Sorcery", "author": "nobody103", "fiction_id": "71045"}
        chapters = feed.parse_syndication(document, feed_config)
        self.assertEqual(10, len(chapters))
        self.assertEqual("Chapter 0", chapters[0]["title"])

    def test_extract_chapter_content(self):
        # Verify chapter HTML extractor strips navigation, watermarks, and author notes.
        page = """
        <html><head><style>.watermark { display: none; }</style></head><body>
          <nav><a href='/previous'>Previous Chapter</a></nav>
          <div class='content-parent'>
            <div class='chapter-inner chapter-content'>
              <p class='random' onclick='bad()'>Full <em>chapter</em>.</p>
              <span class='watermark'>Read this only on Royal Road.</span>
              <p><a href='/map'>Map</a></p>
              <p><a href='/next'>Next Chapter</a></p>&lt;--&gt;
              <img alt='empty'>
              <div class='author-note'>Nested author note.</div>
              <script>bad()</script>
            </div>
            <div class='author-note-portlet'><div class='author-note'>Author note.</div></div>
          </div>
          <div class='comments'>Not chapter content</div>
        </body></html>
        """

        # Extract cleaned chapter body.
        body = feed.extract_chapter(page, "https://www.royalroad.com/fiction/chapter/1")

        # Assert valid chapter HTML remains and unwanted elements are stripped.
        self.assertIn("Full <em>chapter</em>.", body)
        self.assertIn('href="https://www.royalroad.com/map"', body)
        self.assertNotIn("Previous Chapter", body)
        self.assertNotIn("Next Chapter", body)
        self.assertNotIn("&lt;--&gt;", body)
        self.assertNotIn("Read this only on Royal Road", body)
        self.assertNotIn("Not chapter content", body)
        self.assertNotIn("Author note.", body)
        self.assertNotIn("Nested author note.", body)
        self.assertNotIn("onclick", body)
        self.assertNotIn("bad()", body)
        self.assertNotIn("empty", body)

    def test_feed_contains_full_body(self):
        # Verify feed XML contains full chapter HTML within content:encoded element.
        chapter = {
            "title": "Chapter 1",
            "link": "https://www.royalroad.com/fiction/chapter/1",
            "guid": "1",
            "date": feed.parse_date("Tue, 04 Aug 2026 21:00:16 GMT"),
            "body_html": "<p>Complete chapter.</p>",
        }

        # Build feed XML and parse root element.
        feed_config = {
            "key": "zenith-of-sorcery",
            "fiction_id": "71045",
            "slug": "zenith-of-sorcery",
            "title": "Zenith of Sorcery",
            "author": "nobody103",
        }
        xml = feed.build_feed_xml(feed_config, [chapter])
        root = ET.fromstring(xml)
        body = root.find(f"./channel/item/{{{feed.CONTENT_NS}}}encoded")
        self.assertEqual("<p>Complete chapter.</p>", body.text)

    def test_noop_merge_fetches_zero(self):
        # Verify merging already retained chapters incurs zero new network fetches.
        chapters = [self.make_chapter(i, i) for i in range(1, 4)]
        existing = {chapter["link"]: chapter for chapter in chapters}
        loader = Mock()
        listing = [
            {key: value for key, value in chapter.items() if key != "body_html"}
            for chapter in chapters
        ]

        # Merge with mock body loader.
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, fetched, evicted = feed.merge_new_chapters(
                existing, listing, loader
            )

        self.assertEqual(chapters[::-1], merged)
        self.assertEqual((0, 0), (fetched, evicted))
        loader.assert_not_called()

    def test_merge_evicts_oldest_rr(self):
        # Verify newest chapter is fetched and oldest is evicted past limit.
        chapters = [self.make_chapter(i, i) for i in range(1, 4)]
        existing = {chapter["link"]: chapter for chapter in chapters}
        new_chapter = self.make_chapter(4, 4, body=BodyOption.OMIT)
        duplicate_guid = self.make_chapter(
            3, 4, body=BodyOption.OMIT, link_id=300
        )
        stale = self.make_chapter(99, 1, body=BodyOption.OMIT)
        loader = Mock(return_value="<p>Body 4</p>")

        # Merge new chapter into existing set.
        with patch.object(feed, "ITEM_LIMIT", 3):
            merged, fetched, evicted = feed.merge_new_chapters(
                existing,
                [new_chapter, new_chapter, duplicate_guid, stale],
                loader,
            )

        self.assertEqual((1, 1), (fetched, evicted))
        self.assertEqual(["4", "3", "2"], [chapter["guid"] for chapter in merged])
        loader.assert_called_once()
        self.assertEqual("4", loader.call_args.args[0]["guid"])

    def test_prune_untracked_rr_dirs(self):
        # Verify untracked Royal Road directories are deleted while others remain.
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            active_dir = out_dir / "zenith-of-sorcery"
            active_dir.mkdir()
            (active_dir / "feed.xml").write_text(
                "<rss><channel><title>Zenith — Royal Road</title>"
                "<link>https://www.royalroad.com</link></channel></rss>"
            )

            untracked_rr = out_dir / "ponder-the-orb"
            untracked_rr.mkdir()
            (untracked_rr / "feed.xml").write_text(
                "<rss><channel><title>Ponder — Royal Road</title>"
                "<link>https://www.royalroad.com</link></channel></rss>"
            )

            other_dir = out_dir / "cerim"
            other_dir.mkdir()
            (other_dir / "feed.xml").write_text(
                "<rss><channel><title>cerim — Pawchive</title>"
                "<link>https://pawchive.pw</link></channel></rss>"
            )

            # Prune directories not in active keys.
            pruned = feed.prune_untracked_feeds(out_dir, {"zenith-of-sorcery"})
            self.assertEqual(["ponder-the-orb"], pruned)
            self.assertTrue(active_dir.exists())
            self.assertFalse(untracked_rr.exists())
            self.assertTrue(other_dir.exists())


if __name__ == "__main__":
    unittest.main()
