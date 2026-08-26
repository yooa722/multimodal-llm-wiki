from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mmwiki.pipeline import WikiPipeline
from mmwiki.provenance import build_evidence_page_index, evidence_locator_lookup


class EvidencePageIndexTests(unittest.TestCase):
    @staticmethod
    def _state() -> dict:
        return {
            "schema_version": "0.4",
            "sources": {
                "user-document": {
                    "source_version": "version-1",
                    "title": "用户提供的文档",
                    "items": [
                        {
                            "item_id": "item-p0001-b0001",
                            "sequence": 1,
                            "item_type": "title",
                            "page_start": 1,
                            "page_end": 1,
                            "bbox": {"values": [100, 50, 900, 100]},
                            "breadcrumb": "第一章",
                            "raw_text": "第一章",
                            "provenance": {
                                "raw_ref": "content_list_v2[0][0]"
                            },
                            "asset_ids": [],
                        },
                        {
                            "item_id": "item-p0001-b0002",
                            "sequence": 2,
                            "item_type": "paragraph",
                            "page_start": 1,
                            "page_end": 1,
                            "bbox": {"values": [100, 120, 900, 220]},
                            "breadcrumb": "第一章 > 背景",
                            "raw_text": "这是第一页的第一段。",
                            "provenance": {
                                "raw_ref": "content_list_v2[0][1]"
                            },
                            "asset_ids": [],
                        },
                        {
                            "item_id": "item-p0001-b0003",
                            "sequence": 3,
                            "item_type": "paragraph",
                            "page_start": 1,
                            "page_end": 1,
                            "bbox": {"values": [100, 240, 900, 340]},
                            "breadcrumb": "第一章 > 背景",
                            "raw_text": "这是第一页的第二段。",
                            "provenance": {
                                "raw_ref": "content_list_v2[0][2]"
                            },
                            "asset_ids": [],
                        },
                        {
                            "item_id": "item-p0001-b0004",
                            "sequence": 4,
                            "item_type": "image",
                            "page_start": 1,
                            "page_end": 1,
                            "bbox": {"values": [120, 360, 880, 760]},
                            "breadcrumb": "第一章 > 架构",
                            "caption": "系统架构图",
                            "provenance": {
                                "raw_ref": "content_list_v2[0][3]"
                            },
                            "asset_ids": ["image-1"],
                        },
                        {
                            "item_id": "unlocated-item",
                            "sequence": 5,
                            "item_type": "paragraph",
                            "page_start": None,
                            "page_end": None,
                            "bbox": {},
                            "breadcrumb": "附录",
                            "raw_text": "上游没有提供页码。",
                            "provenance": {},
                            "asset_ids": [],
                        },
                    ],
                }
            },
            "pages": {},
            "queries": [],
        }

    def test_index_assigns_page_block_and_paragraph_locations(self) -> None:
        page_index = build_evidence_page_index(self._state())
        lookup = evidence_locator_lookup(page_index)

        first = lookup["user-document@version-1#item-p0001-b0002"]
        second = lookup["user-document@version-1#item-p0001-b0003"]
        image = lookup["user-document@version-1#item-p0001-b0004"]

        self.assertEqual(first["page_index"], 0)
        self.assertEqual(first["page_number"], 1)
        self.assertEqual(first["block_index"], 2)
        self.assertEqual(first["paragraph_index"], 1)
        self.assertEqual(first["location_label"], "第 1 页 · 第 1 段")
        self.assertEqual(second["paragraph_index"], 2)
        self.assertEqual(image["location_label"], "第 1 页 · 图片区域")
        self.assertEqual(image["bbox"]["values"], [120, 360, 880, 760])
        self.assertEqual(page_index["stats"]["unlocated_items"], 1)
        self.assertEqual(
            page_index["sources"]["user-document"]["unlocated_items"][0][
                "item_id"
            ],
            "unlocated-item",
        )

    def test_pipeline_persists_generic_page_index_and_query_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))
            state = self._state()
            pipeline._save_state(state)

            persisted = json.loads(
                pipeline.page_index_path.read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["schema_version"], "mmwiki-page-index-0.1")
            self.assertEqual(persisted["stats"]["located_items"], 4)

            hits = [
                {
                    "source_id": "user-document",
                    "item_ids": ["item-p0001-b0003"],
                    "matched_asset_path": "",
                }
            ]
            selected, _, _ = pipeline._select_query_evidence(hits, state)
            self.assertEqual(
                selected[0]["locator"]["location_label"],
                "第 1 页 · 第 2 段",
            )
            evidence = pipeline._query_evidence_index(selected)
            self.assertEqual(evidence[0]["location"]["block_index"], 3)


if __name__ == "__main__":
    unittest.main()
