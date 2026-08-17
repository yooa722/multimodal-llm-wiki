from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mmwiki.web import media_url, render_wiki_html, resolve_vault_path, wiki_view_url


class WikiWebTests(unittest.TestCase):
    def test_urls_are_http_and_quote_unicode_paths(self) -> None:
        wiki = wiki_view_url("wiki/analyses/智慧交通项目评审分析.md")
        media = media_url("assets/中文_006/example image.jpg")
        self.assertTrue(wiki.startswith("http://127.0.0.1:19828/wiki/view?path="))
        self.assertIn("%E6%99%BA", wiki)
        self.assertIn("example%20image.jpg", media)

    def test_resolve_vault_path_rejects_escape_and_wrong_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "assets/source/image.jpg"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"jpeg")
            self.assertEqual(
                resolve_vault_path(
                    root,
                    "assets/source/image.jpg",
                    required_prefix="assets",
                    allowed_suffixes={".jpg"},
                ),
                asset.resolve(),
            )
            with self.assertRaises(ValueError):
                resolve_vault_path(
                    root,
                    "../secret.jpg",
                    required_prefix="assets",
                    allowed_suffixes={".jpg"},
                )
            with self.assertRaises(ValueError):
                resolve_vault_path(
                    root,
                    "wiki/page.md",
                    required_prefix="assets",
                    allowed_suffixes={".jpg"},
                )

    def test_renderer_converts_wikilink_image_table_and_anchor(self) -> None:
        rendered = render_wiki_html(
            """---
title: "测试页面"
---
# 测试页面

[[wiki/concepts/概念|打开概念]]

<a id="item-1"></a>

| 阶段 | 预算 |
| --- | --- |
| 开发 | 300 |

![[assets/source/image.jpg]]
""",
            "wiki/test.md",
            "http://127.0.0.1:19828",
        ).decode("utf-8")
        self.assertIn("/wiki/view?path=wiki/concepts/", rendered)
        self.assertIn("/api/v1/media/assets/source/image.jpg", rendered)
        self.assertIn('<span id="item-1"></span>', rendered)
        self.assertIn("<table>", rendered)


if __name__ == "__main__":
    unittest.main()
