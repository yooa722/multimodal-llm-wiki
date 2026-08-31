from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mmwiki.web import (
    media_url,
    query_view_url,
    render_query_html,
    render_wiki_html,
    resolve_vault_path,
    wiki_view_url,
)
from mmwiki.api import PRESENTATION_VERSION


class WikiWebTests(unittest.TestCase):
    def test_api_advertises_current_presentation_version(self) -> None:
        self.assertEqual(PRESENTATION_VERSION, "split-query-v3")

    def test_urls_are_http_and_quote_unicode_paths(self) -> None:
        wiki = wiki_view_url("wiki/analyses/智慧交通项目评审分析.md")
        media = media_url("assets/中文_006/example image.jpg")
        self.assertTrue(wiki.startswith("http://127.0.0.1:19828/wiki/view?path="))
        self.assertIn("%E6%99%BA", wiki)
        self.assertIn("example%20image.jpg", media)

    def test_query_workspace_places_answer_left_and_wiki_right(self) -> None:
        evidence_id = "source@v1#item-1"
        record = {
            "query_id": "query-test",
            "question": "预算是多少？",
            "answer": f"预算为300万元〔{evidence_id}〕。",
            "evidence_refs": [evidence_id],
            "evidence_locations": [
                {
                    "evidence_id": evidence_id,
                    "page_number": 1,
                    "paragraph_index": None,
                    "location_label": "第 1 页 · 表格区域",
                    "breadcrumb": "项目实施 > 工期与预算",
                }
            ],
            "citations": [
                {
                    "source_id": "source",
                    "title": "工期与预算",
                    "evidence_ids": [evidence_id],
                    "item_ids": ["item-1"],
                    "pages": [1],
                    "modalities": ["table"],
                    "path": "wiki/sources/source.md",
                    "wiki_paths": ["wiki/analyses/项目分析.md"],
                }
            ],
            "retrieval": {
                "mode": "hybrid",
                "wiki_navigation": [
                    {
                        "path": "wiki/analyses/项目分析.md",
                        "title": "项目分析",
                    }
                ],
            },
            "model": "test-model",
        }

        rendered = render_query_html(
            record,
            "http://127.0.0.1:19828",
            evidence=1,
            view="wiki",
        ).decode("utf-8")

        self.assertIn("grid-template-columns", rendered)
        self.assertIn("问答与证据核验", rendered)
        self.assertIn("预算为300万元", rendered)
        self.assertIn("Evidence 1", rendered)
        self.assertIn("项目分析", rendered)
        self.assertIn("<iframe", rendered)
        self.assertIn("wiki/analyses/%E9%A1%B9%E7%9B%AE%E5%88%86%E6%9E%90.md", rendered)
        self.assertIn("Wiki 页面", rendered)
        self.assertIn("原始 Evidence", rendered)
        self.assertIn("第 1 页 · 表格区域", rendered)
        self.assertIn("项目实施 &gt; 工期与预算", rendered)
        self.assertIn("/query/view?id=query-test", query_view_url("query-test"))

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

    def test_renderer_preserves_standard_markdown_image_alt(self) -> None:
        rendered = render_wiki_html(
            "![系统架构图](assets/figure.png)",
            "wiki/test.md",
            "http://127.0.0.1:19828",
        ).decode("utf-8")
        self.assertIn('alt="系统架构图"', rendered)
        self.assertIn("/api/v1/media/assets/figure.png", rendered)

        nested = render_wiki_html(
            "![派生 Caption](../assets/asset-figure.png)",
            "wiki/external/demo/pages/readme.md",
            "http://127.0.0.1:19828",
        ).decode("utf-8")
        self.assertIn("/api/v1/media/wiki/external/demo/assets/asset-figure.png", nested)


if __name__ == "__main__":
    unittest.main()
