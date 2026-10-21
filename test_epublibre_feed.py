from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import epublibre_feed as feed


LISTING_HTML = """
<div class="row">
  <a id="stk" href="https://www.epublibre.org/libro/detalle/85251"
     class="negrita popover-libro" rel="popover" title="The Game-Players of Titan"
     data-content='<div class="txt_pequena"><div class="pull-right sprite idioma_5"></div>
     <img class="sprite user_silhouette" src="/img/vacio.png" /> Philip K. Dick<br/>
     <div class="row-fluid"><div class="span3"><img class="sprite icon-revision"
     src="/img/vacio.png" /> 1.0</div><div class="span3"><img class="sprite icon-paginas"
     src="/img/vacio.png" /> 163</div></div>'>
    <div class="portada-catalogo"><img id="catalog" src="https://images.epublibre.org/libros/85251.jpg" /></div>
    <div class="etiqueta6"><img class="sprite estado-2" src="/img/vacio.png"/></div>
  </a>
</div>
"""

DETAIL_HTML = """
<html><body>
  <img id="portada" src="https://images.epublibre.org/libros/85251.jpg" />
  <div class="titulo">The Game-Players of Titan</div>
  <div class="detalle">
    <div class="negrita">Sinopsis</div>
    <div class="ali_justi"><span>First paragraph.<br><br>Second <i>paragraph</i>.</span></div>
    <div class="ali_justi"><a href="https://www.epublibre.org/coleccion/index/1">Titan [EN]</a></div>
    <table>
      <tr><td class="celda-rotulo"><div class="label">Escritor</div></td>
          <td><a href="https://www.epublibre.org/autor/index/34">Philip K. Dick</a></td></tr>
    </table>
    <table class="negrita">
      <tr><td><a href="https://www.epublibre.org/genero/index/genero/1/3">Novela</a></td></tr>
      <tr><td><a href="https://www.epublibre.org/genero/index/subgenero/1/21">Ciencia ficción</a></td></tr>
    </table>
  </div>
  <table>
    <tr>
      <td>Páginas:</td><td><span>163</span></td>
      <td>En la web desde:</td><td><span>10-10-2026</span></td>
      <td>Publicado en:</td><td><span>1964</span></td>
    </tr>
    <tr>
      <td>Revisión:</td><td><span>1.0</span></td>
      <td>Estado:</td><td><span>Disponible</span></td>
    </tr>
  </table>
  <a href="magnet:?xt=urn:btih:ABC123&amp;dn=EPL_The_Game-Players_of_Titan"
     id="en_desc">Descargar (magnet link)</a>
</body></html>
"""


class EpubLibreFeedTests(unittest.TestCase):
    # Unit tests for the ePubLibre catalogue feed builder.

    def test_feed_defs_unique(self):
        # Verify the single feed configuration has a unique key.
        self.assertEqual(1, len(feed.FEEDS))
        self.assertEqual(1, len({item["key"] for item in feed.FEEDS}))

    def test_parse_listing(self):
        # Verify catalogue tiles yield id, link, author, revision, and cover.
        books = feed.parse_listing(LISTING_HTML)
        self.assertEqual(1, len(books))
        self.assertEqual("85251", books[0]["id"])
        self.assertEqual("https://www.epublibre.org/libro/detalle/85251", books[0]["link"])
        self.assertEqual("The Game-Players of Titan", books[0]["title"])
        self.assertEqual("Philip K. Dick", books[0]["author"])
        self.assertEqual("1.0", books[0]["revision"])
        self.assertEqual("163", books[0]["pages"])
        self.assertEqual("https://images.epublibre.org/libros/85251.jpg", books[0]["cover"])

    def test_parse_detail(self):
        # Verify detail parsing keeps date, synopsis, genres, and magnet link.
        book = {
            "id": "85251",
            "link": feed.detail_url("85251"),
            "title": "fallback",
            "author": "fallback",
            "cover": "",
            "pages": "",
            "revision": "",
        }
        detail = feed.parse_detail(DETAIL_HTML, book)
        self.assertEqual("The Game-Players of Titan", detail["title"])
        self.assertEqual("Philip K. Dick", detail["author"])
        self.assertEqual("1964", detail["year"])
        self.assertEqual("163", detail["pages"])
        self.assertEqual("Disponible", detail["state"])
        self.assertEqual(dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc), detail["date"])
        self.assertIn("First paragraph.", detail["synopsis"])
        self.assertIn("Second paragraph.", detail["synopsis"])
        self.assertEqual(["Titan [EN]"], detail["collections"])
        self.assertEqual(["Novela", "Ciencia ficción"], detail["genres"])
        self.assertTrue(detail["magnet"].startswith("magnet:?xt=urn:btih:"))

    def test_parse_detail_dedupes_authors(self):
        # Verify repeated author rows produce one name.
        document = DETAIL_HTML.replace(
            "</table>",
            '<tr><td class="celda-rotulo"><div class="label">Escritor</div></td>'
            '<td><a href="https://www.epublibre.org/autor/index/34">'
            "Philip K. Dick</a></td></tr></table>",
            1,
        )
        book = {
            "id": "85251",
            "link": feed.detail_url("85251"),
            "title": "x",
            "author": "x",
            "cover": "",
            "pages": "",
            "revision": "",
        }

        detail = feed.parse_detail(document, book)

        self.assertEqual("Philip K. Dick", detail["author"])

    def test_item_body_includes_magnet(self):
        # Verify rendered body carries magnet link, cover, and escaped synopsis.
        book = {
            "title": "Title",
            "author": "Author",
            "link": feed.detail_url("85251"),
            "cover": "https://images.epublibre.org/libros/85251.jpg",
            "synopsis": "A & B\n\nSecond paragraph.",
            "collections": ["Titan [EN]"],
            "genres": ["Novela"],
            "pages": "163",
            "year": "1964",
            "revision": "1.0",
            "state": "Disponible",
            "magnet": "magnet:?xt=urn:btih:ABC123",
        }
        body = feed.item_body(book)
        self.assertIn('href="magnet:?xt=urn:btih:ABC123"', body)
        self.assertIn("images.epublibre.org/libros/85251.jpg", body)
        self.assertIn("A &amp; B", body)
        self.assertIn("<p>Second paragraph.</p>", body)
        self.assertIn("Genres: Novela", body)

    def test_collect_stops_at_known(self):
        # Verify collection walks pages only above the first published entry.
        page_one = [
            {"id": str(100 - i), "link": feed.detail_url(str(100 - i))} for i in range(18)
        ]
        page_two = [
            {"id": str(80 - i), "link": feed.detail_url(str(80 - i))} for i in range(18)
        ]
        existing = {feed.detail_url("95"): {}}

        def fake_book(session, book):
            return {**book, "date": dt.datetime.now(feed.rss.UTC)}

        with patch.object(
            feed, "fetch_listing", Mock(side_effect=[page_one, page_two])
        ), patch.object(feed, "fetch_book", side_effect=fake_book):
            fresh = feed.collect_new(Mock(), existing)

        self.assertEqual(
            [book["link"] for book in page_one[:5]], [book["link"] for book in fresh]
        )

    def test_run_keeps_same_day_addition(self):
        # Verify a same-day book is retained although dates carry no clock time.
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            day = (dt.datetime.now(feed.rss.UTC) - dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            old_link = feed.detail_url("84836")
            new_link = feed.detail_url("85251")
            old = {
                "title": "Old - Author",
                "link": old_link,
                "date": day,
                "body_html": "<p>Old</p>",
            }
            new = {
                "id": "85251",
                "link": new_link,
                "title": "New",
                "author": "Author",
                "cover": "",
                "synopsis": "Synopsis.",
                "collections": [],
                "genres": [],
                "pages": "10",
                "year": "2000",
                "revision": "1.0",
                "state": "Disponible",
                "date": day,
                "magnet": "magnet:?xt=urn:btih:ABC123",
            }

            with patch.object(feed, "OUT_DIR", out_dir), patch.object(
                feed, "ITEM_LIMIT", 10
            ), patch.object(
                feed, "load_existing", Mock(return_value={old_link: old})
            ), patch.object(
                feed, "collect_new", Mock(return_value=[new])
            ), patch.object(
                feed, "fetch_book", Mock(return_value=new)
            ):
                count = feed.run_feed(Mock(), feed.FEEDS[0])

            self.assertEqual(2, count)
            root = ET.parse(out_dir / "epublibre" / "en" / "feed.xml").getroot()
            titles = [item.findtext("title") for item in root.findall("./channel/item")]
            self.assertEqual(["New - Author", "Old - Author"], titles)

    def test_run_drops_expired_entries(self):
        # Verify items older than the four-week window leave the feed.
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            old_link = feed.detail_url("80000")
            new_link = feed.detail_url("85251")
            expired = {
                "title": "Expired - Author",
                "link": old_link,
                "date": dt.datetime.now(feed.rss.UTC) - dt.timedelta(days=40),
                "body_html": "<p>Expired</p>",
            }
            new = {
                "id": "85251",
                "link": new_link,
                "title": "New",
                "author": "Author",
                "cover": "",
                "synopsis": "Synopsis.",
                "collections": [],
                "genres": [],
                "pages": "10",
                "year": "2000",
                "revision": "1.0",
                "state": "Disponible",
                "date": dt.datetime.now(feed.rss.UTC),
                "magnet": "magnet:?xt=urn:btih:ABC123",
            }

            with patch.object(feed, "OUT_DIR", out_dir), patch.object(
                feed, "load_existing", Mock(return_value={old_link: expired})
            ), patch.object(
                feed, "collect_new", Mock(return_value=[new])
            ):
                count = feed.run_feed(Mock(), feed.FEEDS[0])

            self.assertEqual(1, count)
            root = ET.parse(out_dir / "epublibre" / "en" / "feed.xml").getroot()
            titles = [item.findtext("title") for item in root.findall("./channel/item")]
            self.assertEqual(["New - Author"], titles)

    def test_fetch_book_skips_blank_page(self):
        # Verify a restricted/empty 200 detail page yields None, not a bad book.
        response = Mock(content=b"\n\n", text="\n\n")
        with patch.object(feed.rss, "fetch", Mock(return_value=response)):
            book = feed.fetch_book(Mock(), {"link": feed.detail_url("1")})

        self.assertIsNone(book)

    def test_collect_skips_single_blank_page(self):
        # Verify one restricted book is skipped while the rest are collected.
        listing = [{"link": feed.detail_url(str(i))} for i in range(3)]
        recent = dt.datetime.now(feed.rss.UTC)

        def fake_book(session, book):
            if book["link"].endswith("/1"):
                return None
            return {**book, "date": recent}

        with patch.object(feed, "fetch_listing", Mock(return_value=listing)), patch.object(
            feed, "fetch_book", side_effect=fake_book
        ):
            fresh = feed.collect_new(Mock(), {})

        self.assertEqual(
            [feed.detail_url("0"), feed.detail_url("2")],
            [book["link"] for book in fresh],
        )

    def test_collect_aborts_on_blank_run(self):
        # Verify three blank pages in a row are treated as throttling.
        listing = [{"link": feed.detail_url(str(i))} for i in range(3)]
        with patch.object(feed, "fetch_listing", Mock(return_value=listing)), patch.object(
            feed, "fetch_book", Mock(return_value=None)
        ):
            with self.assertRaises(RuntimeError):
                feed.collect_new(Mock(), {})

    def test_collect_rejects_empty_catalogue(self):
        # Verify an empty first page never wipes an existing feed.
        with patch.object(feed, "fetch_listing", Mock(return_value=[])):
            with self.assertRaises(RuntimeError):
                feed.collect_new(Mock(), {})

    def test_prune_untracked_epl_dirs(self):
        # Verify untracked ePubLibre directories are deleted, others survive.
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            active = out_dir / "epublibre"
            active.mkdir()
            (active / "feed.xml").write_text(
                "<rss><channel><link>https://www.epublibre.org</link></channel></rss>"
            )
            stale = out_dir / "old"
            stale.mkdir()
            (stale / "feed.xml").write_text(
                "<rss><channel><title>ePubLibre English</title></channel></rss>"
            )
            other = out_dir / "cerim"
            other.mkdir()
            (other / "feed.xml").write_text(
                "<rss><channel><link>https://pawchive.pw</link></channel></rss>"
            )

            pruned = feed.prune_untracked_feeds(out_dir, {"epublibre"})

            self.assertEqual(["old"], pruned)
            self.assertTrue(active.exists())
            self.assertFalse(stale.exists())
            self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
