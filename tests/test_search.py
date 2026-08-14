from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from mmwiki.retrieval import (
    HybridRetriever,
    RETRIEVAL_INDEX_VERSION,
    RetrievalIndex,
    cosine_similarity,
)
from mmwiki.search import Retriever, navigate_wiki, reference_labels, tokens


class TokenizationTests(unittest.TestCase):
    def test_chinese_bigrams_and_latin_terms_are_preserved(self) -> None:
        value = tokens("开发测试 Qwen3-VL Form 7004")
        self.assertTrue({"开发", "发测", "测试", "qwen3-vl", "form", "7004"} <= value)

    def test_figure_and_table_labels_are_recognized(self) -> None:
        self.assertEqual(reference_labels("比较 Figure 4 与 Table 1"), {"figure 4", "table 1"})


class RetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "sources": {
                "source-a": {
                    "title": "项目计划",
                    "wiki_path": "wiki/sources/source-a.md",
                    "assets": {},
                    "chunks": [
                        {
                            "chunk_id": "chunk-table",
                            "breadcrumb": "工期与预算",
                            "text": "开发测试阶段 120天 15人 300万元",
                            "item_ids": ["item-table"],
                            "modalities": ["table"],
                            "asset_ids": [],
                            "page_refs": [1],
                        },
                        {
                            "chunk_id": "chunk-text",
                            "breadcrumb": "正文",
                            "text": "开发测试阶段说明",
                            "item_ids": ["item-text"],
                            "modalities": ["text"],
                            "asset_ids": [],
                            "page_refs": [1],
                        },
                    ],
                },
                "source-b": {
                    "title": "其他来源",
                    "wiki_path": "wiki/sources/source-b.md",
                    "assets": {},
                    "chunks": [
                        {
                            "chunk_id": "chunk-other",
                            "breadcrumb": "预算",
                            "text": "开发测试阶段 90天",
                            "item_ids": ["item-other"],
                            "modalities": ["table"],
                            "asset_ids": [],
                            "page_refs": [2],
                        }
                    ],
                },
            }
        }

    def test_table_intent_boosts_table_chunk(self) -> None:
        hits = Retriever(self.state).search(
            "开发测试阶段的表格预算是多少？", 5, {"source-a"}
        )
        self.assertEqual(hits[0].chunk_id, "chunk-table")
        self.assertIn("table", hits[0].modalities)

    def test_source_filter_excludes_other_packages(self) -> None:
        hits = Retriever(self.state).search("开发测试阶段", 5, {"source-b"})
        self.assertEqual([hit.chunk_id for hit in hits], ["chunk-other"])


class FakeRetrievalProvider:
    text_embedding_model = "fake-text-embedding"
    text_rerank_model = "fake-text-rerank"
    vl_embedding_model = "fake-vl-embedding"
    vl_rerank_model = "fake-vl-rerank"
    text_configured = True
    multimodal_configured = True

    def text_embeddings(self, texts):
        return [[1.0, 0.0] for _ in texts], {"total_tokens": len(texts)}

    def text_rerank(self, query, documents, top_n):
        return [
            {"index": index, "relevance_score": 1.0 - index * 0.1}
            for index in range(min(top_n, len(documents)))
        ], {"total_tokens": len(documents)}

    def multimodal_embedding(self, contents, fused):
        return [0.0, 1.0], {"total_tokens": 1}

    def multimodal_rerank(self, query, documents, top_n):
        self.multimodal_rerank_documents = documents
        return [
            {"index": index, "relevance_score": 0.95 - index * 0.1}
            for index in range(min(top_n, len(documents)))
        ], {"total_tokens": len(documents)}


class HybridRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        asset = self.vault / "assets/figure.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"fake-png")
        source_page = self.vault / "wiki/sources/source-a.md"
        source_page.parent.mkdir(parents=True)
        source_page.write_text(
            "# English source\n\nedge computing architecture and Figure 4 flow",
            encoding="utf-8",
        )
        self.state = {
            "sources": {
                "source-a": {
                    "title": "English source",
                    "source_version": "version-a",
                    "wiki_path": "wiki/sources/source-a.md",
                    "assets": {
                        "asset-1": {
                            "vault_path": "assets/figure.png",
                            "media_type": "image/png",
                        }
                    },
                    "chunks": [
                        {
                            "chunk_id": "chunk-text",
                            "breadcrumb": "Architecture",
                            "text": "edge computing architecture",
                            "item_ids": ["item-text"],
                            "modalities": ["text"],
                            "asset_ids": [],
                            "page_refs": [1],
                        },
                        {
                            "chunk_id": "chunk-image",
                            "breadcrumb": "Figure 4",
                            "text": "visual retrieval flow",
                            "item_ids": ["item-image"],
                            "modalities": ["image"],
                            "asset_ids": ["asset-1"],
                            "page_refs": [2],
                        },
                    ],
                }
            }
        }
        self.index_path = self.vault / "retrieval-index.json"
        self.index_path.write_text(
            json.dumps(
                {
                    "schema_version": RETRIEVAL_INDEX_VERSION,
                    "sources": {"source-a": "version-a"},
                    "text": {
                        "model": "fake-text-embedding",
                        "records": [
                            {
                                "source_id": "source-a",
                                "chunk_id": "chunk-text",
                                "vector": [1.0, 0.0],
                            },
                            {
                                "source_id": "source-a",
                                "chunk_id": "chunk-image",
                                "vector": [0.0, 1.0],
                            },
                        ],
                    },
                    "visual": {
                        "model": "fake-vl-embedding",
                        "records": [
                            {
                                "source_id": "source-a",
                                "chunk_id": "chunk-image",
                                "asset_id": "asset-1",
                                "asset_path": "assets/figure.png",
                                "vector": [0.0, 1.0],
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cosine_similarity(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_index_builds_text_and_visual_records(self) -> None:
        status = RetrievalIndex(self.index_path, self.vault).build(
            self.state, FakeRetrievalProvider(), include_visual=True
        )
        self.assertTrue(status["text_ready"])
        self.assertTrue(status["visual_ready"])
        self.assertEqual(status["text_records"], 2)
        self.assertEqual(status["visual_records"], 1)

    def test_index_incrementally_adds_only_selected_source(self) -> None:
        state = json.loads(json.dumps(self.state))
        state["sources"]["source-b"] = {
            "title": "New source",
            "source_version": "version-b",
            "wiki_path": "wiki/sources/source-b.md",
            "assets": {},
            "chunks": [
                {
                    "chunk_id": "chunk-new",
                    "breadcrumb": "New",
                    "text": "new evidence",
                    "item_ids": ["item-new"],
                    "modalities": ["text"],
                    "asset_ids": [],
                    "page_refs": [1],
                }
            ],
        }
        provider = FakeRetrievalProvider()
        status = RetrievalIndex(self.index_path, self.vault).build(
            state,
            provider,
            include_visual=True,
            source_ids={"source-b"},
        )
        value = json.loads(self.index_path.read_text(encoding="utf-8"))

        self.assertTrue(status["fresh"])
        self.assertTrue(status["incremental"])
        self.assertEqual(status["indexed_source_ids"], ["source-b"])
        self.assertEqual(status["text_records"], 3)
        self.assertEqual(status["visual_records"], 1)
        self.assertEqual(
            {record["source_id"] for record in value["text"]["records"]},
            {"source-a", "source-b"},
        )

    def test_incremental_index_rejects_stale_retained_source(self) -> None:
        state = json.loads(json.dumps(self.state))
        state["sources"]["source-a"]["source_version"] = "changed-version"
        state["sources"]["source-b"] = {
            "title": "New source",
            "source_version": "version-b",
            "assets": {},
            "chunks": [],
        }

        with self.assertRaisesRegex(Exception, "缺失或已过期"):
            RetrievalIndex(self.index_path, self.vault).build(
                state,
                FakeRetrievalProvider(),
                include_visual=True,
                source_ids={"source-b"},
            )

    def test_wiki_navigation_reads_source_pages_before_chunk_retrieval(self) -> None:
        pages = navigate_wiki(self.state, self.vault, "Figure 4 flow")
        self.assertEqual(pages[0]["path"], "wiki/sources/source-a.md")
        self.assertEqual(pages[0]["source_ids"], ["source-a"])

    def test_semantic_wiki_navigation_aggregates_chunk_vectors_by_source(self) -> None:
        records = [
            {"source_id": "source-a", "vector": [1.0, 0.0]},
            {"source_id": "source-a", "vector": [0.9, 0.1]},
            {"source_id": "source-b", "vector": [0.0, 1.0]},
        ]

        ranked = HybridRetriever._rank_wiki_sources(
            [1.0, 0.0], records, None, limit=2
        )

        self.assertEqual([value["source_id"] for value in ranked], ["source-a", "source-b"])

    def test_hybrid_search_uses_text_embedding_and_rerank(self) -> None:
        hits, trace = HybridRetriever(
            self.state, self.vault, self.index_path
        ).search(
            "中文语义问题",
            2,
            None,
            {},
            None,
            "hybrid",
            FakeRetrievalProvider(),
        )
        self.assertEqual(hits[0].chunk_id, "chunk-text")
        self.assertEqual(hits[0].source_id, "source-a")
        self.assertIn("text_embedding", hits[0].retrieval_channels)
        self.assertIn("text_rerank", hits[0].retrieval_channels)
        self.assertEqual(trace["mode"], "hybrid")

    def test_multimodal_search_uses_visual_embedding_and_rerank(self) -> None:
        provider = FakeRetrievalProvider()
        hits, trace = HybridRetriever(
            self.state, self.vault, self.index_path
        ).search(
            "图中是什么？",
            2,
            None,
            {},
            {"source-a": 1},
            "multimodal",
            provider,
        )
        self.assertTrue(any("multimodal_embedding" in hit.retrieval_channels for hit in hits))
        self.assertTrue(any("multimodal_rerank" in hit.retrieval_channels for hit in hits))
        self.assertTrue(provider.multimodal_rerank_documents)
        self.assertTrue(
            all(
                isinstance(document, dict)
                and set(document).issubset({"text", "image"})
                for document in provider.multimodal_rerank_documents
            )
        )
        self.assertEqual(trace["mode"], "multimodal")
        self.assertIn("wiki_navigation", trace["channels"])

    def test_multimodal_search_preserves_the_specific_matched_image(self) -> None:
        second = self.vault / "assets/second.png"
        second.write_bytes(b"second-image")
        source = self.state["sources"]["source-a"]
        source["assets"]["asset-2"] = {
            "vault_path": "assets/second.png",
            "media_type": "image/png",
        }
        source["chunks"][1]["asset_ids"].append("asset-2")
        index = json.loads(self.index_path.read_text(encoding="utf-8"))
        index["visual"]["records"] = [
            {
                "source_id": "source-a",
                "chunk_id": "chunk-image",
                "asset_id": "asset-2",
                "asset_path": "assets/second.png",
                "vector": [0.0, 1.0],
            }
        ]
        self.index_path.write_text(json.dumps(index), encoding="utf-8")
        provider = FakeRetrievalProvider()

        hits, _ = HybridRetriever(self.state, self.vault, self.index_path).search(
            "图中是什么？", 2, None, {}, None, "multimodal", provider
        )

        visual_hit = next(hit for hit in hits if hit.chunk_id == "chunk-image")
        self.assertEqual(visual_hit.matched_asset_id, "asset-2")
        self.assertEqual(visual_hit.matched_asset_path, "assets/second.png")
        self.assertTrue(
            any(
                document.get("image", "").endswith("c2Vjb25kLWltYWdl")
                for document in provider.multimodal_rerank_documents
                if isinstance(document, dict)
            )
        )


if __name__ == "__main__":
    unittest.main()
