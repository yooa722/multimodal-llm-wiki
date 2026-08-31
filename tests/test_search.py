from __future__ import annotations

import unittest
from copy import deepcopy
import hashlib
import http.client
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from mmwiki.retrieval import (
    BailianRetrievalProvider,
    HybridRetriever,
    LEGACY_RETRIEVAL_INDEX_VERSION,
    RETRIEVAL_INDEX_VERSION,
    RetrievalIndex,
    _source_fingerprints,
    cosine_similarity,
)
from mmwiki.search import Retriever, navigate_wiki, reference_labels, tokens


class TokenizationTests(unittest.TestCase):
    def test_chinese_bigrams_and_latin_terms_are_preserved(self) -> None:
        value = tokens("开发测试 Qwen3-VL Form 7004")
        self.assertTrue({"开发", "发测", "测试", "qwen3-vl", "form", "7004"} <= value)

    def test_figure_and_table_labels_are_recognized(self) -> None:
        self.assertEqual(reference_labels("比较 Figure 4 与 Table 1"), {"figure 4", "table 1"})


class RetrievalProviderTransportTests(unittest.TestCase):
    def test_post_retries_incomplete_chunked_response(self) -> None:
        provider = object.__new__(BailianRetrievalProvider)
        provider.key = "test-key"
        provider.timeout = 1
        provider.retries = 3

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"data": [{"embedding": [1.0]}]}).encode("utf-8")

        with (
            patch(
                "mmwiki.retrieval.urllib.request.urlopen",
                side_effect=[http.client.IncompleteRead(b"partial"), Response()],
            ) as post,
            patch("mmwiki.retrieval.time.sleep") as sleep,
        ):
            value = provider._post("https://example.invalid/embeddings", {"input": ["a"]})

        self.assertEqual(value["data"][0]["embedding"], [1.0])
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_text_embeddings_use_small_parallel_batches_and_keep_order(self) -> None:
        provider = object.__new__(BailianRetrievalProvider)
        provider.text_embedding_model = "test-model"
        provider.text_embedding_url = "https://example.invalid/embeddings"
        provider.dimension = 2
        provider.text_embedding_batch_size = 2
        provider.text_embedding_workers = 2
        calls: list[list[str]] = []

        def fake_post(url, body):
            batch = list(body["input"])
            calls.append(batch)
            return {
                "data": [
                    {"index": index, "embedding": [float(text), 0.0]}
                    for index, text in enumerate(batch)
                ],
                "usage": {"total_tokens": len(batch)},
            }

        provider._post = fake_post
        vectors, usage = provider.text_embeddings(["1", "2", "3", "4", "5"])

        self.assertEqual([vector[0] for vector in vectors], [1, 2, 3, 4, 5])
        self.assertEqual(sorted(len(batch) for batch in calls), [1, 2, 2])
        self.assertEqual(usage["total_tokens"], 5)


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

    def test_image_ocr_is_searchable_and_returns_parent_image(self) -> None:
        self.state["sources"]["source-a"].update(
            {
                "source_version": "v1",
                "visual_evidence": [
                    {
                        "id": "source-a@v1#asset-1#image_ocr",
                        "kind": "image_ocr",
                        "text": "Layer 14 附近的 Recall@1 约为 6.8%。",
                        "asset_id": "asset-1",
                        "parent_item_ids": ["item-image"],
                        "parent_chunk_ids": ["chunk-image"],
                        "page_refs": [2],
                        "status": "ready",
                        "searchable": True,
                    }
                ],
            }
        )
        self.state["sources"]["source-a"]["assets"] = {
            "asset-1": {"vault_path": "assets/figure.png"}
        }

        hits = Retriever(self.state).search("6.8", 5, {"source-a"})

        self.assertEqual(hits[0].chunk_id, "source-a@v1#asset-1#image_ocr")
        self.assertEqual(hits[0].item_ids, ["item-image"])
        self.assertEqual(hits[0].asset_paths, ["assets/figure.png"])


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
                    "source_fingerprints": _source_fingerprints(self.state),
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

    def test_empty_visual_evidence_preserves_legacy_source_fingerprint(self) -> None:
        source = self.state["sources"]["source-a"]
        legacy_payload = {
            "source_version": "version-a",
            "representation": "legacy",
            "chunks": [
                {
                    "chunk_id": str(chunk.get("chunk_id") or ""),
                    "text": str(chunk.get("text") or ""),
                    "asset_ids": list(map(str, chunk.get("asset_ids", []))),
                }
                for chunk in source["chunks"]
            ],
            "assets": {
                str(asset_id): {
                    "sha256": str(asset.get("sha256") or ""),
                    "vault_path": str(asset.get("vault_path") or ""),
                }
                for asset_id, asset in sorted(source["assets"].items())
            },
        }
        expected = hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        state = deepcopy(self.state)
        state["sources"]["source-a"]["visual_evidence"] = []

        self.assertEqual(_source_fingerprints(self.state)["source-a"], expected)
        self.assertEqual(_source_fingerprints(state)["source-a"], expected)

    def test_index_builds_text_and_visual_records(self) -> None:
        status = RetrievalIndex(self.index_path, self.vault).build(
            self.state, FakeRetrievalProvider(), include_visual=True
        )
        self.assertTrue(status["text_ready"])
        self.assertTrue(status["visual_ready"])
        self.assertTrue(status["wiki_semantic_ready"])
        self.assertEqual(status["text_records"], 2)
        self.assertEqual(status["visual_records"], 1)
        self.assertEqual(status["wiki_records"], 1)

    def test_full_rebuild_reuses_unchanged_vectors(self) -> None:
        index = RetrievalIndex(self.index_path, self.vault)
        index.build(self.state, FakeRetrievalProvider(), include_visual=True)

        status = index.build(self.state, FakeRetrievalProvider(), include_visual=True)

        self.assertEqual(status["reused_text_records"], 2)
        self.assertEqual(status["new_text_records"], 0)
        self.assertEqual(status["reused_wiki_records"], 1)
        self.assertEqual(status["new_wiki_records"], 0)
        self.assertEqual(status["reused_visual_records"], 1)
        self.assertEqual(status["new_visual_records"], 0)

    def test_derived_visual_evidence_enters_text_index_not_visual_index(self) -> None:
        state = deepcopy(self.state)
        state["sources"]["source-a"]["visual_evidence"] = [
            {
                "id": "source-a@version-a#asset-1#image_ocr",
                "kind": "image_ocr",
                "text": "Recall@1 6.8%",
                "asset_id": "asset-1",
                "parent_item_ids": ["item-image"],
                "parent_chunk_ids": ["chunk-image"],
                "page_refs": [2],
                "status": "ready",
                "searchable": True,
            }
        ]
        state["sources"]["source-a"]["source_version"] = "version-a"

        status = RetrievalIndex(self.index_path, self.vault).build(
            state, FakeRetrievalProvider(), include_visual=True
        )

        self.assertTrue(status["text_ready"])
        self.assertEqual(status["text_records"], 3)
        self.assertEqual(status["visual_records"], 1)

    def test_visual_index_only_embeds_assets_with_ready_visual_evidence(self) -> None:
        state = deepcopy(self.state)
        source = state["sources"]["source-a"]
        second_asset = self.vault / "assets/other.png"
        second_asset.write_bytes(b"other-png")
        source["assets"]["asset-2"] = {
            "vault_path": "assets/other.png",
            "media_type": "image/png",
        }
        source["chunks"].append(
            {
                "chunk_id": "chunk-image-2",
                "breadcrumb": "Figure 5",
                "text": "decorative image",
                "item_ids": ["item-image-2"],
                "modalities": ["image"],
                "asset_ids": ["asset-2"],
                "page_refs": [3],
            }
        )
        source["visual_evidence"] = [
            {
                "id": "source-a@version-a#asset-1#image_caption",
                "kind": "image_caption",
                "text": "Figure 4 visual retrieval flow",
                "asset_id": "asset-1",
                "parent_item_ids": ["item-image"],
                "parent_chunk_ids": ["chunk-image"],
                "page_refs": [2],
                "status": "ready",
                "searchable": True,
            }
        ]

        status = RetrievalIndex(self.index_path, self.vault).build(
            state, FakeRetrievalProvider(), include_visual=True
        )
        value = json.loads(self.index_path.read_text(encoding="utf-8"))

        self.assertEqual(status["visual_records"], 1)
        self.assertEqual(status["eligible_visual_records"], 1)
        self.assertEqual(value["visual"]["records"][0]["asset_id"], "asset-1")

    def test_wiki_page_backfill_preserves_existing_evidence_vectors(self) -> None:
        index = RetrievalIndex(self.index_path, self.vault)
        index.build(self.state, FakeRetrievalProvider(), include_visual=True)
        value = json.loads(self.index_path.read_text(encoding="utf-8"))
        expected_text = value["text"]["records"]
        expected_visual = value["visual"]["records"]
        value.pop("wiki")
        self.index_path.write_text(json.dumps(value), encoding="utf-8")

        status = index.build_wiki_pages(self.state, FakeRetrievalProvider())
        rebuilt = json.loads(self.index_path.read_text(encoding="utf-8"))

        self.assertTrue(status["wiki_semantic_ready"])
        self.assertEqual(
            status["index_scope"], "wiki_pages_with_existing_evidence_vectors"
        )
        self.assertEqual(status["new_wiki_records"], 1)
        self.assertEqual(status["preserved_text_records"], 2)
        self.assertEqual(status["preserved_visual_records"], 1)
        self.assertEqual(rebuilt["text"]["records"], expected_text)
        self.assertEqual(rebuilt["visual"]["records"], expected_visual)

    def test_wiki_page_index_can_bootstrap_without_evidence_vectors(self) -> None:
        self.index_path.unlink()
        index = RetrievalIndex(self.index_path, self.vault)

        status = index.build_wiki_pages(self.state, FakeRetrievalProvider())
        value = json.loads(self.index_path.read_text(encoding="utf-8"))

        self.assertTrue(status["wiki_semantic_ready"])
        self.assertFalse(status["text_ready"])
        self.assertEqual(status["index_scope"], "wiki_pages_only_lightweight")
        self.assertEqual(value["text"]["records"], [])
        self.assertEqual(status["new_wiki_records"], 1)

    def test_hybrid_search_uses_page_vectors_with_local_evidence(self) -> None:
        self.index_path.unlink()
        index = RetrievalIndex(self.index_path, self.vault)
        provider = FakeRetrievalProvider()
        index.build_wiki_pages(self.state, provider)
        retriever = HybridRetriever(self.state, self.vault, self.index_path)

        hits, trace = retriever.search(
            "edge computing architecture",
            2,
            None,
            {"source-a": ["wiki/sources/source-a.md"]},
            None,
            "hybrid",
            provider,
        )

        self.assertTrue(hits)
        self.assertEqual(trace["mode"], "hybrid")
        self.assertIn("wiki_page_embedding", trace["channels"])
        self.assertIn("轻量索引", trace["fallback_reason"])

    def test_legacy_index_migration_preserves_vectors_without_api_calls(self) -> None:
        value = json.loads(self.index_path.read_text(encoding="utf-8"))
        value["schema_version"] = LEGACY_RETRIEVAL_INDEX_VERSION
        value.pop("source_fingerprints", None)
        self.index_path.write_text(json.dumps(value), encoding="utf-8")

        status = RetrievalIndex(self.index_path, self.vault).migrate_legacy(
            self.state, FakeRetrievalProvider()
        )

        self.assertEqual(status["status"], "migrated")
        self.assertTrue(status["fresh"])
        self.assertEqual(status["external_api_calls"], 0)
        self.assertEqual(status["preserved_text_records"], 2)
        self.assertEqual(status["preserved_visual_records"], 1)

    def test_index_incrementally_adds_only_selected_source(self) -> None:
        RetrievalIndex(self.index_path, self.vault).build(
            self.state, FakeRetrievalProvider(), include_visual=True
        )
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
        self.assertEqual(status["reused_text_records"], 2)
        self.assertEqual(status["new_text_records"], 1)
        self.assertEqual(status["reused_visual_records"], 1)
        self.assertEqual(status["new_visual_records"], 0)

    def test_multimodal_upgrade_reuses_text_base_vectors(self) -> None:
        text_state = json.loads(json.dumps(self.state))
        source = text_state["sources"]["source-a"]
        source["representation"] = "text"
        source["chunks"] = [source["chunks"][0]]
        source["assets"] = {}
        index = RetrievalIndex(self.index_path, self.vault)

        text_status = index.build(
            text_state, FakeRetrievalProvider(), include_visual=False
        )
        self.assertEqual(text_status["new_text_records"], 1)
        self.assertEqual(text_status["visual_records"], 0)

        rich_state = json.loads(json.dumps(self.state))
        rich_state["sources"]["source-a"]["representation"] = "text+multimodal"
        rich_status = index.build(
            rich_state,
            FakeRetrievalProvider(),
            include_visual=True,
            source_ids={"source-a"},
        )

        self.assertTrue(rich_status["fresh"])
        self.assertEqual(rich_status["reused_text_records"], 1)
        self.assertEqual(rich_status["new_text_records"], 1)
        self.assertEqual(rich_status["reused_visual_records"], 0)
        self.assertEqual(rich_status["new_visual_records"], 1)

    def test_incremental_index_rejects_stale_retained_source(self) -> None:
        RetrievalIndex(self.index_path, self.vault).build(
            self.state, FakeRetrievalProvider(), include_visual=True
        )
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

    def test_semantic_wiki_navigation_ranks_pages_before_sources(self) -> None:
        pages = HybridRetriever._rank_wiki_pages(
            [1.0, 0.0],
            [
                {
                    "path": "wiki/concepts/edge.md",
                    "title": "Edge",
                    "summary": "",
                    "kind": "concept",
                    "source_ids": ["source-a"],
                    "vector": [1.0, 0.0],
                },
                {
                    "path": "wiki/concepts/other.md",
                    "title": "Other",
                    "summary": "",
                    "kind": "concept",
                    "source_ids": ["source-b"],
                    "vector": [0.0, 1.0],
                },
            ],
            None,
        )

        self.assertEqual(pages[0]["path"], "wiki/concepts/edge.md")
        self.assertEqual(pages[0]["navigation_stage"], "wiki-page-embedding")

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
        index["source_fingerprints"] = _source_fingerprints(self.state)
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
