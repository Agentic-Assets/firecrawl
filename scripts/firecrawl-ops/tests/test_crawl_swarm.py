#!/usr/bin/env python3
"""Fixture-only tests for crawl_swarm.py.

Run from the repo root:

    python3 scripts/firecrawl-ops/tests/test_crawl_swarm.py
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SWARM_PATH = Path(__file__).resolve().parents[1] / "crawl_swarm.py"


def load_swarm_module():
    spec = importlib.util.spec_from_file_location("crawl_swarm", SWARM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


swarm = load_swarm_module()


class CrawlSwarmHelperTests(unittest.TestCase):
    def test_read_lines_skips_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seeds.txt"
            path.write_text("\n# comment\nhttps://example.com\n  https://second.test  \n", encoding="utf-8")
            self.assertEqual(swarm.read_lines(path), ["https://example.com", "https://second.test"])

    def test_normalize_links_accepts_multiple_shapes_and_deduplicates(self) -> None:
        links = swarm.normalize_links(
            [
                "https://example.com/a#frag",
                {"url": "https://example.com/a"},
                {"link": "https://example.com/b"},
                {"href": "https://example.com/c#section"},
                {"href": ""},
                None,
            ]
        )

        self.assertEqual(
            links,
            ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
        )

    def test_links_from_html_resolves_relative_links(self) -> None:
        html = """
        <a href="/people/jane">Jane</a>
        <a href="https://example.com/team#bio">Team</a>
        <a href="">Ignore</a>
        """

        self.assertEqual(
            swarm.links_from_html("https://example.com/base/page", html),
            ["https://example.com/people/jane", "https://example.com/team"],
        )

    def test_domain_and_canonical_url_helpers(self) -> None:
        self.assertTrue(swarm.same_domain("https://example.com/a", "https://example.com/root"))
        self.assertFalse(swarm.same_domain("https://other.test/a", "https://example.com/root"))
        self.assertEqual(
            swarm.canonical_url_key("https://example.com/path/index.php?x=1#frag"),
            "https://example.com/path",
        )

    def test_sort_expanded_links_prioritizes_people_detail_pages(self) -> None:
        ranked = swarm.sort_expanded_links(
            [
                "https://example.com/about",
                "https://example.com/team",
                "https://example.com/profile/jane",
                "https://example.com/faculty/bob",
            ],
            r"faculty",
        )

        self.assertEqual(
            ranked,
            [
                "https://example.com/profile/jane",
                "https://example.com/team",
                "https://example.com/faculty/bob",
                "https://example.com/about",
            ],
        )

    def test_map_seed_normalizes_firecrawl_links(self) -> None:
        with patch.object(
            swarm,
            "post_json",
            return_value={
                "success": True,
                "links": [
                    {"url": "https://example.com/a#frag"},
                    {"href": "https://example.com/b"},
                    "https://example.com/a",
                ],
            },
        ) as post_json:
            seed, links = swarm.map_seed("http://local/v2", "https://example.com", 5)

        self.assertEqual(seed, "https://example.com")
        self.assertEqual(links, ["https://example.com/a", "https://example.com/b"])
        post_json.assert_called_once_with("http://local/v2/map", {"url": "https://example.com", "limit": 5}, timeout=180)

    def test_scrape_url_combines_api_links_with_html_links(self) -> None:
        with patch.object(
            swarm,
            "post_json",
            return_value={
                "success": True,
                "data": {
                    "markdown": "hello",
                    "rawHtml": '<a href="/team">Team</a>',
                    "links": ["https://example.com/contact"],
                },
            },
        ):
            result = swarm.scrape_url("http://local/v2", "https://example.com/about")

        self.assertEqual(result["url"], "https://example.com/about")
        self.assertTrue(result["success"])
        self.assertEqual(result["markdown_len"], 5)
        self.assertEqual(
            result["links"],
            ["https://example.com/contact", "https://example.com/team"],
        )


if __name__ == "__main__":
    unittest.main()
