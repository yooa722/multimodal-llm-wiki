from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from mmwiki.contracts import load_package
from mmwiki.pipeline import PipelineError, WikiPipeline


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def make_package(root: Path) -> Path:
    package = root / "package"
    (package / "assets").mkdir(parents=True)
    (package / "assets/figure.png").write_bytes(b"test-image")
    manifest = {
        "schema_version": "mmwiki-0.1",
        "package_id": "staged-demo",
        "document": {
            "title": "Staged Demo",
            "source": {"filename": "demo.pdf", "media_type": "application/pdf"},
        },
        "parser": {"name": "test", "version": "1"},
        "artifacts": {
            "items": "items.jsonl",
            "chunks": "chunks.jsonl",
            "assets_index": "assets.json",
        },
    }
    (package / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    items = [
        {
            "item_id": "title-1",
            "sequence": 1,
            "type": "title",
            "page_start": 1,
            "page_end": 1,
            "bbox": {},
            "breadcrumb": "Demo",
            "content": {
                "raw_text": "Demo Title",
                "caption": "",
                "search_text": "Demo Title",
                "semantic": {},
            },
            "assets": [],
            "provenance": {},
            "quality": {},
            "retrieval": {"searchable": True, "exclude": False},
        },
        {
            "item_id": "paragraph-1",
            "sequence": 2,
            "type": "paragraph",
            "page_start": 1,
            "page_end": 1,
            "bbox": {},
            "breadcrumb": "Demo",
            "content": {
                "raw_text": "Text fact",
                "caption": "",
                "search_text": "Text fact",
                "semantic": {},
            },
            "assets": [],
            "provenance": {},
            "quality": {},
            "retrieval": {"searchable": True, "exclude": False},
        },
        {
            "item_id": "table-1",
            "sequence": 3,
            "type": "table",
            "page_start": 1,
            "page_end": 1,
            "bbox": {},
            "breadcrumb": "Demo",
            "content": {
                "raw_text": "",
                "caption": "",
                "search_text": "指标 数值 A 10",
                "semantic": {},
                "table": {"rows": [["指标", "数值"], ["A", "10"]]},
            },
            "assets": [],
            "provenance": {},
            "quality": {},
            "retrieval": {"searchable": True, "exclude": False},
        },
        {
            "item_id": "image-1",
            "sequence": 4,
            "type": "image",
            "page_start": 1,
            "page_end": 1,
            "bbox": {},
            "breadcrumb": "Demo",
            "content": {
                "raw_text": "",
                "caption": "Architecture figure",
                "search_text": "Architecture figure",
                "semantic": {},
            },
            "assets": [{"asset_id": "asset-1"}],
            "provenance": {},
            "quality": {},
            "retrieval": {"searchable": True, "exclude": False},
        },
    ]
    write_jsonl(package / "items.jsonl", items)
    write_jsonl(
        package / "chunks.jsonl",
        [
            {
                "chunk_id": f"chunk-{index}",
                "item_ids": [item["item_id"]],
                "text": item["content"]["search_text"],
                "breadcrumb": "Demo",
                "modalities": [item["type"]],
                "asset_ids": [
                    value["asset_id"] for value in item.get("assets", [])
                ],
                "page_refs": [1],
                "provenance": {},
                "quality": {},
            }
            for index, item in enumerate(items, 1)
        ],
    )
    (package / "assets.json").write_text(
        json.dumps(
            [
                {
                    "asset_id": "asset-1",
                    "path": "assets/figure.png",
                    "media_type": "image/png",
                }
            ]
        ),
        encoding="utf-8",
    )
    return package


class StagedIngestTests(unittest.TestCase):
    def test_visual_evidence_builds_ocr_and_caption_once_per_asset(self) -> None:
        class FakeOCR:
            calls = 0
            model = "qwen3.5-ocr"
            task = "text_recognition"
            configured = True

            def __init__(self, root):
                pass

            def recognize(self, data_url):
                FakeOCR.calls += 1
                return "Layer 14 Recall@1 6.8%", {"total_tokens": 1}

        class FakeVision:
            calls = 0
            configured = True
            model = "fake-vlm"

            def analyze_wiki(self, title, evidence, catalog, schema, images, stage):
                FakeVision.calls += 1
                return {
                    "summary": "视觉摘要",
                    "claims": [
                        {
                            "statement": "图中存在 Recall 曲线",
                            "evidence_refs": [evidence[-1]["id"]],
                            "provenance": "extracted",
                        }
                    ],
                    "entities": [],
                    "concepts": [],
                    "contradictions": [],
                    "page_actions": [],
                    "image_annotations": [
                        {
                            "asset_id": images[0]["asset_id"],
                            "evidence_id": images[0]["evidence_id"],
                            "caption": "不同层的 Recall@1 曲线",
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = load_package(make_package(root))
            pipeline = WikiPipeline(root)
            assets = pipeline._copy_assets(package, {"asset-1"})
            evidence = pipeline._builder_evidence(package, {"image-1"})
            with patch("mmwiki.pipeline.QwenOCRProvider", FakeOCR):
                first = pipeline._build_visual_evidence(
                    package,
                    evidence,
                    assets,
                    {"pages": {}},
                    "schema",
                    FakeVision(),
                )
                second = pipeline._build_visual_evidence(
                    package,
                    evidence,
                    assets,
                    {"pages": {}},
                    "schema",
                    FakeVision(),
                )

            self.assertEqual(FakeOCR.calls, 1)
            self.assertEqual(FakeVision.calls, 1)
            self.assertEqual(first[3], 2)
            self.assertEqual(second[3], 0)
            self.assertEqual(
                {record["kind"] for record in first[0]},
                {"image_caption", "image_ocr"},
            )
            self.assertTrue(all(record["searchable"] for record in first[0]))

    def test_existing_page_context_includes_evidence_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = WikiPipeline(root)
            page_path = pipeline.vault / "wiki/concepts/Page.md"
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text("# Page\n\n旧正文", encoding="utf-8")
            state = {
                "pages": {
                    "wiki/concepts/Page.md": {
                        "title": "Page",
                        "kind": "concept",
                        "path": "wiki/concepts/Page.md",
                        "source_ids": ["staged-demo"],
                        "evidence_ids": ["staged-demo@v1#text-1"],
                    }
                }
            }

            existing = pipeline._existing_pages_for_actions(
                {"page_actions": [{"title": "Page", "kind": "concept"}]},
                state,
            )

            self.assertEqual(existing[0]["evidence_ids"], ["staged-demo@v1#text-1"])

    def test_preserved_evidence_ids_are_limited_to_current_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = load_package(make_package(root))
            pipeline = WikiPipeline(root)
            current_text_id = pipeline._item_evidence_id(package, "paragraph-1")
            foreign_id = "other-source@v1#text-1"

            preserved = pipeline._preserved_evidence_ids(
                package,
                [{"evidence_ids": [current_text_id, foreign_id]}],
            )

            self.assertEqual(preserved, {current_text_id})

    def test_multimodal_stage_requires_text_wiki_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = make_package(root)
            pipeline = WikiPipeline(root)

            with self.assertRaisesRegex(PipelineError, "文本 LLM Wiki 基座"):
                pipeline.ingest(package, stage="multimodal")

    def test_multimodal_evidence_is_added_after_text_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = make_package(root)
            pipeline = WikiPipeline(root)

            text_result = pipeline.ingest(package, stage="text")
            text_state = pipeline._load_state()["sources"]["staged-demo"]
            text_page = (pipeline.vault / text_state["wiki_path"]).read_text(
                encoding="utf-8"
            )

            self.assertEqual(text_result["stage"], "text")
            self.assertEqual(text_state["representation"], "text")
            self.assertEqual(
                {item["item_type"] for item in text_state["items"]},
                {"title", "paragraph", "text"},
            )
            self.assertEqual(len(text_state["items"]), 4)
            self.assertEqual(len(text_state["chunks"]), 4)
            self.assertTrue(
                all(chunk["modalities"] == ["text"] for chunk in text_state["chunks"])
            )
            self.assertTrue(all(not item["table"] for item in text_state["items"]))
            self.assertTrue(all(not item["asset_ids"] for item in text_state["items"]))
            self.assertEqual(text_state["assets"], {})
            self.assertNotIn("指标 | 数值", text_page)
            self.assertIn("Architecture figure", text_page)
            self.assertNotIn("![[", text_page)

            multimodal_result = pipeline.ingest(package, stage="multimodal")
            multimodal_state = pipeline._load_state()["sources"]["staged-demo"]
            multimodal_page = (
                pipeline.vault / multimodal_state["wiki_path"]
            ).read_text(encoding="utf-8")

            self.assertEqual(multimodal_result["stage"], "multimodal")
            self.assertEqual(multimodal_state["representation"], "text+multimodal")
            self.assertEqual(len(multimodal_state["items"]), 4)
            self.assertEqual(len(multimodal_state["assets"]), 1)
            self.assertEqual(
                set(multimodal_state["stages"]), {"text", "multimodal"}
            )
            self.assertIn("| 指标 | 数值 |", multimodal_page)
            self.assertIn("Architecture figure", multimodal_page)
            self.assertIn("![[assets/staged-demo/asset-1.png]]", multimodal_page)
            self.assertEqual(
                multimodal_result["build_metrics"]["multimodal_items_added"], 2
            )

            unchanged = pipeline.ingest(package, stage="multimodal")
            self.assertEqual(unchanged["status"], "unchanged")
            self.assertEqual(unchanged["build_metrics"]["api_calls"], 0)


if __name__ == "__main__":
    unittest.main()
