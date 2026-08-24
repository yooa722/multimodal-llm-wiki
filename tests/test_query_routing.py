from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mmwiki.config import FeatureConfig
from mmwiki.pipeline import WikiPipeline


def search_result(mode: str) -> dict:
    asset_path = "assets/source/image.png"
    return {
        "hits": [
            {
                "source_id": "source",
                "chunk_id": "chunk-1",
                "title": "Figure",
                "score": 1.0,
                "snippet": "A caption",
                "item_ids": ["image-1"],
                "modalities": ["image"],
                "asset_paths": [asset_path],
                "pages": [1],
                "path": "wiki/sources/source.md",
                "wiki_paths": [],
                "retrieval_channels": ["text_rerank" if mode == "hybrid" else "multimodal_rerank"],
                "score_breakdown": {},
                "matched_asset_id": "asset-1" if mode == "multimodal" else "",
                "matched_asset_path": asset_path if mode == "multimodal" else "",
            }
        ],
        "retrieval": {
            "requested_mode": "auto" if mode == "hybrid" else "multimodal",
            "initial_mode": mode,
            "mode": mode,
            "channels": ["text_rerank" if mode == "hybrid" else "multimodal_rerank"],
            "models": {},
            "usage": {},
            "fallback_reason": None,
            "routing_reason": "test",
            "visual_intent": {"is_visual": False, "categories": []},
        },
    }


class QueryRoutingTests(unittest.TestCase):
    def make_pipeline(self, root: Path) -> WikiPipeline:
        pipeline = WikiPipeline(root)
        pipeline.features = FeatureConfig(
            enable_vlm=True, enable_vector_retrieval=True
        )
        asset_path = pipeline.vault / "assets/source/image.png"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(b"image")
        pipeline._save_state(
            {
                "schema_version": "0.4",
                "sources": {
                    "source": {
                        "title": "Source",
                        "source_version": "v1",
                        "wiki_path": "wiki/sources/source.md",
                        "items": [
                            {
                                "item_id": "image-1",
                                "item_type": "image",
                                "breadcrumb": "Figure",
                                "page_start": 1,
                                "raw_text": "",
                                "caption": "A caption",
                                "search_text": "A caption",
                                "table": None,
                                "asset_ids": ["asset-1"],
                            }
                        ],
                        "assets": {
                            "asset-1": {
                                "vault_path": "assets/source/image.png",
                                "media_type": "image/png",
                            }
                        },
                    }
                },
                "pages": {},
                "queries": [],
            }
        )
        return pipeline

    def test_answerable_hybrid_query_does_not_send_image_pixels(self) -> None:
        calls: list[tuple[str, int]] = []

        class FakeAnswerProvider:
            def __init__(self, root, task):
                self.task = task
                self.model = f"fake-{task}"

            def answer(self, question, evidence, images):
                calls.append((self.task, len(images)))
                return {
                    "answer": "Caption 已足够回答。",
                    "answerable": True,
                    "evidence_refs": ["source@v1#image-1"],
                    "_usage": {"total_tokens": 1},
                }

        with tempfile.TemporaryDirectory() as directory:
            pipeline = self.make_pipeline(Path(directory))
            with (
                patch.object(
                    pipeline, "search_with_trace", return_value=search_result("hybrid")
                ),
                patch("mmwiki.pipeline.OpenAICompatibleProvider", FakeAnswerProvider),
            ):
                record = pipeline.query("这个条目的标题是什么？", provider="api")

        self.assertEqual(calls, [("answer", 0)])
        self.assertEqual(record["retrieval"]["mode"], "hybrid")
        self.assertEqual(record["answer_mode"], "text_generation")

    def test_unanswerable_hybrid_query_with_image_upgrades_once(self) -> None:
        calls: list[tuple[str, int]] = []

        class FakeAnswerProvider:
            def __init__(self, root, task):
                self.task = task
                self.model = f"fake-{task}"

            def answer(self, question, evidence, images):
                calls.append((self.task, len(images)))
                if self.task == "answer":
                    return {
                        "answer": "当前文本证据不足。",
                        "answerable": False,
                        "evidence_refs": [],
                        "_usage": {"total_tokens": 1},
                    }
                return {
                    "answer": "读取原图后可以回答。",
                    "answerable": True,
                    "evidence_refs": ["source@v1#image-1"],
                    "_usage": {"total_tokens": 2},
                }

        with tempfile.TemporaryDirectory() as directory:
            pipeline = self.make_pipeline(Path(directory))
            with (
                patch.object(
                    pipeline,
                    "search_with_trace",
                    side_effect=[search_result("hybrid"), search_result("multimodal")],
                ),
                patch("mmwiki.pipeline.OpenAICompatibleProvider", FakeAnswerProvider),
            ):
                record = pipeline.query("这个对象是什么？", provider="api")

        self.assertEqual(calls, [("answer", 0), ("vision", 1)])
        self.assertEqual(record["retrieval"]["mode"], "multimodal")
        self.assertEqual(
            record["retrieval"]["mode_transition"], ["hybrid", "multimodal"]
        )
        self.assertIn("证据不足", record["retrieval"]["upgrade_reason"])
        self.assertEqual(record["answer_mode"], "multimodal_generation")
        self.assertEqual(record["usage"]["total_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
