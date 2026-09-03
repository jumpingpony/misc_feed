#!/usr/bin/env python3
"""Regression tests for OPML feed outline manifests."""
from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

OPML_DIR = Path(__file__).resolve().parent / "OPML"
EXPECTED_BASE_URL = "https://jumpingpony.github.io/misc_feed/"
LEGACY_OWNER = "nappingcats"


class OPMLValidationTests(unittest.TestCase):
    """Validate structure, syntax, and target URLs of OPML files."""

    def test_opml_files_exist(self) -> None:
        """Ensure all required OPML files are present in the directory."""
        expected_files = {"all.opml", "pawchive.opml", "royalroad.opml"}
        actual_files = {p.name for p in OPML_DIR.glob("*.opml")}

        # Check required files exist
        self.assertTrue(expected_files.issubset(actual_files))

    def test_opml_xml_validity(self) -> None:
        """Parse all OPML files to verify strict XML compliance."""
        opml_files = list(OPML_DIR.glob("*.opml"))
        self.assertGreater(len(opml_files), 0)

        for path in opml_files:
            # Parse XML and extract root element
            tree = ET.parse(path)
            root = tree.getroot()

            self.assertEqual(root.tag, "opml")
            self.assertIsNotNone(root.find("head"))
            self.assertIsNotNone(root.find("body"))

    def test_feed_urls_point_to_jumpingpony(self) -> None:
        """Verify each outline item uses the jumpingpony GitHub Pages domain."""
        opml_files = list(OPML_DIR.glob("*.opml"))

        for path in opml_files:
            tree = ET.parse(path)
            outlines = tree.findall(".//outline[@xmlUrl]")

            self.assertGreater(len(outlines), 0, f"No feed outlines in {path.name}")

            for outline in outlines:
                xml_url = outline.attrib.get("xmlUrl", "")

                # Assert new domain base
                self.assertTrue(
                    xml_url.startswith(EXPECTED_BASE_URL),
                    f"{path.name} contains invalid xmlUrl: {xml_url}",
                )

                # Assert legacy owner is not present
                self.assertNotIn(
                    LEGACY_OWNER,
                    xml_url,
                    f"{path.name} contains legacy owner in xmlUrl: {xml_url}",
                )


if __name__ == "__main__":
    unittest.main()
