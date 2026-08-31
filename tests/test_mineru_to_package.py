from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mmwiki.contracts import load_package, validate_package
from tools.mineru_to_package import (
    convert_content_list,
    discover_content_lists,
    iter_mineru_blocks,
)


class MinerUToPackageTests(unittest.TestCase):
    def test_discovery_prefers_v2_for_the_same_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "demo_content_list.json").write_text("[]", encoding="utf-8")
            v2 = root / "demo_content_list_v2.json"
            v2.write_text("[]", encoding="utf-8")
            self.assertEqual(discover_content_lists(root), [v2.resolve()])

    def test_v2_conversion_preserves_location_and_visual_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "mineru"
            output = root / "packages"
            (source / "images").mkdir(parents=True)
            (source / "images" / "figure.png").write_bytes(b"not-a-real-png")
            value = [
                [
                    {
                        "type": "title",
                        "bbox": [10, 20, 900, 80],
                        "content": {"title_content": "系统架构", "level": 1},
                    },
                    {
                        "type": "paragraph",
                        "bbox": [10, 100, 900, 180],
                        "content": {"paragraph_content": "图 1 展示完整的数据处理流程。"},
                    },
                    {
                        "type": "image",
                        "sub_type": "flowchart",
                        "bbox": [10, 200, 900, 700],
                        "content": {
                            "image_path": "images/figure.png",
                            "image_caption": ["图 1 系统流程"],
                        },
                    },
                    {
                        "type": "paragraph",
                        "bbox": [10, 720, 900, 800],
                        "content": {"paragraph_content": "流程先解析文档，再构建 Wiki。"},
                    },
                ]
            ]
            path = source / "demo_content_list_v2.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

            package_path = convert_content_list(path, output, context_chars=200)
            result = validate_package(package_path)
            package = load_package(package_path)

            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["counts"], {"items": 4, "chunks": 4, "assets": 1})
            visual = package.items[2]
            self.assertEqual(visual.page_start, 1)
            self.assertEqual(visual.bbox["values"], [10, 200, 900, 700])
            self.assertIn("数据处理流程", visual.semantic["adjacent_text"])
            self.assertIn("构建 Wiki", visual.semantic["adjacent_text"])
            visual_chunk = package.chunks[2]
            self.assertEqual(visual_chunk.item_ids, [visual.item_id])
            self.assertEqual(
                visual_chunk.provenance["context_item_ids"],
                ["item-p0001-b0002", "item-p0001-b0004"],
            )

    def test_legacy_flat_content_list_uses_page_idx(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "legacy_content_list.json"
            path.write_text(
                json.dumps(
                    [
                        {"type": "text", "text": "第一段", "page_idx": 2, "bbox": [1, 2, 3, 4]},
                        {
                            "type": "equation",
                            "text": "x^2+y^2",
                            "page_idx": 2,
                            "bbox": [5, 6, 7, 8],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            blocks = iter_mineru_blocks(path)
            self.assertEqual([block.page_number for block in blocks], [3, 3])
            self.assertEqual(blocks[1].raw_ref, "content_list[1]")

            package = load_package(convert_content_list(path, root / "packages"))
            self.assertEqual(package.items[0].page_start, 3)
            self.assertEqual(package.items[1].equation["latex"], "x^2+y^2")

    def test_missing_visual_placeholder_is_preserved_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "placeholder_content_list_v2.json"
            path.write_text(
                json.dumps(
                    [
                        [
                            {
                                "type": "table",
                                "bbox": [10, 20, 900, 800],
                                "content": {
                                    "image_source": {"path": "images/"},
                                    "html": "",
                                },
                            }
                        ]
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            package = load_package(convert_content_list(path, root / "packages"))

            self.assertEqual(len(package.items), 1)
            self.assertEqual(package.items[0].asset_ids, [])
            self.assertTrue(package.items[0].quality["needs_review"])
            self.assertIn("资源不存在", package.items[0].quality["review_reasons"][0])


if __name__ == "__main__":
    unittest.main()
