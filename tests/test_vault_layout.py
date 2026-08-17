from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mmwiki.pipeline import NON_WIKI_VAULT_DOCUMENTS, WikiPipeline


class VaultLayoutTests(unittest.TestCase):
    def test_layout_bootstraps_wiki_purpose_schema_and_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))

            self.assertTrue(pipeline.purpose_path.is_file())
            self.assertTrue(pipeline.schema_path.is_file())
            self.assertTrue(pipeline.maintenance_path.is_file())
            home = (pipeline.vault / "Home.md").read_text(encoding="utf-8")
            self.assertIn("[[wiki/maintenance|Wiki Maintenance]]", home)
            self.assertIn("[[wiki-purpose|Wiki Purpose]]", home)

    def test_process_documents_are_removed_from_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "runtime/vault"
            vault.mkdir(parents=True)
            for filename in NON_WIKI_VAULT_DOCUMENTS:
                path = vault / filename
                path.write_text("temporary", encoding="utf-8")

            WikiPipeline(root)

            self.assertTrue(
                all(not (vault / filename).exists() for filename in NON_WIKI_VAULT_DOCUMENTS)
            )

    def test_home_only_links_to_navigation_sources_and_wiki_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = WikiPipeline(root)
            pipeline._write_navigation(
                {
                    "sources": {
                        "source-a": {
                            "title": "来源 A",
                            "wiki_path": "wiki/sources/source-a.md",
                        }
                    },
                    "pages": {
                        "page-a": {
                            "title": "概念 A",
                            "path": "wiki/concepts/page-a.md",
                        }
                    },
                }
            )
            home = (pipeline.vault / "Home.md").read_text(encoding="utf-8")

            self.assertIn("[[wiki/index|Wiki Index]]", home)
            self.assertIn("[[wiki/sources/source-a|来源 A]]", home)
            self.assertIn("[[wiki/concepts/page-a|概念 A]]", home)
            self.assertNotIn("阶段总结", home)
            self.assertNotIn("Demo验收", home)
            self.assertNotIn("Demo-Questions", home)

    def test_lint_reports_stable_page_coverage_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = WikiPipeline(root)
            pipeline._save_state(
                {
                    "schema_version": "0.2",
                    "sources": {
                        "source-a": {
                            "title": "来源 A",
                            "source_version": "v1",
                            "wiki_path": "wiki/sources/source-a.md",
                            "items": [],
                            "assets": {},
                        }
                    },
                    "pages": {},
                    "queries": [],
                }
            )
            source_page = pipeline.vault / "wiki/sources/source-a.md"
            source_page.parent.mkdir(parents=True, exist_ok=True)
            source_page.write_text("# 来源 A", encoding="utf-8")

            result = pipeline.lint()

            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                result["wiki_coverage"]["stable_page_source_coverage"], 0
            )
            self.assertTrue(result["warnings"])

    def test_search_rejects_invalid_limits_and_unknown_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))

            with self.assertRaisesRegex(Exception, "top_k"):
                pipeline.search_with_trace("问题", top_k=0)
            with self.assertRaisesRegex(Exception, "source_id"):
                pipeline.search_with_trace(
                    "问题", top_k=5, source_ids={"missing-source"}
                )

    def test_navigation_generates_a_multimodal_evidence_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))
            asset = pipeline.vault / "assets/source-a/figure.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"png")
            state = {
                "sources": {
                    "source-a": {
                        "title": "来源 A",
                        "source_version": "v1",
                        "wiki_path": "wiki/sources/source-a.md",
                        "evidence_map_path": "wiki/evidence/source-a-multimodal.md",
                        "items": [
                            {
                                "item_id": "figure-1",
                                "item_type": "figure",
                                "page_start": 2,
                                "breadcrumb": "方法",
                                "caption": "Figure 1 模型架构",
                                "semantic": {"description": "箭头展示数据流"},
                                "table": None,
                                "equation": None,
                                "asset_ids": ["asset-1"],
                            }
                        ],
                        "assets": {
                            "asset-1": {
                                "vault_path": "assets/source-a/figure.png",
                                "media_type": "image/png",
                            }
                        },
                    }
                },
                "pages": {},
            }

            pipeline._write_navigation(state)

            content = (
                pipeline.vault / "wiki/evidence/source-a-multimodal.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Figure 1 模型架构", content)
            self.assertIn("上游语义说明（派生信息，不替代原图）", content)
            self.assertIn("![[assets/source-a/figure.png]]", content)
            self.assertIn("source-a@v1#figure-1", content)

    def test_graph_health_reports_wanted_and_orphan_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))
            page_a = pipeline.vault / "wiki/concepts/a.md"
            page_b = pipeline.vault / "wiki/concepts/b.md"
            page_a.write_text("# A\n\n[[B]] [[Missing]]", encoding="utf-8")
            page_b.write_text("# B", encoding="utf-8")
            state = {
                "sources": {},
                "pages": {
                    "wiki/concepts/a.md": {
                        "title": "A",
                        "kind": "concept",
                        "path": "wiki/concepts/a.md",
                    },
                    "wiki/concepts/b.md": {
                        "title": "B",
                        "kind": "concept",
                        "path": "wiki/concepts/b.md",
                    },
                },
            }

            graph = pipeline._wiki_graph(state)

            self.assertIn("Missing", graph["wanted_pages"])
            self.assertIn("wiki/concepts/a.md", graph["orphans"])
            self.assertNotIn("wiki/concepts/b.md", graph["orphans"])

    def test_refresh_pages_is_local_and_restores_managed_visual_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))
            asset = pipeline.vault / "assets/source-a/figure.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"png")
            page_path = pipeline.vault / "wiki/concepts/page-a.md"
            page_path.write_text(
                "---\ntitle: Page A\n---\n\n# Page A\n\n"
                "---\nsource_ids: [source-a]\n---\n\n# Page A\n\n"
                "正文 [[source-a@v1#figure-1]]\n\n## Evidence\n\n旧证据",
                encoding="utf-8",
            )
            pipeline._save_state(
                {
                    "schema_version": "0.3",
                    "sources": {
                        "source-a": {
                            "title": "来源 A",
                            "source_version": "v1",
                            "wiki_path": "wiki/sources/source-a.md",
                            "model": "build-model",
                            "analysis_model": "vision-model",
                            "items": [
                                {
                                    "item_id": "figure-1",
                                    "item_type": "figure",
                                    "page_start": 1,
                                    "breadcrumb": "架构",
                                    "caption": "架构图",
                                    "semantic": {},
                                    "asset_ids": ["asset-1"],
                                }
                            ],
                            "assets": {
                                "asset-1": {
                                    "vault_path": "assets/source-a/figure.png",
                                    "media_type": "image/png",
                                }
                            },
                        }
                    },
                    "pages": {
                        "wiki/concepts/page-a.md": {
                            "title": "Page A",
                            "kind": "concept",
                            "path": "wiki/concepts/page-a.md",
                            "summary": "页面摘要",
                            "source_ids": ["source-a"],
                            "source_versions": ["v1"],
                            "evidence_ids": ["source-a@v1#figure-1"],
                        }
                    },
                    "queries": [],
                }
            )

            result = pipeline.refresh_wiki_pages()
            content = page_path.read_text(encoding="utf-8")
            refreshed_page = pipeline._load_state()["pages"]["wiki/concepts/page-a.md"]

            self.assertEqual(result["external_api_calls"], 0)
            self.assertEqual(content.count("\n---\n"), 1)
            self.assertEqual(content.count("# Page A"), 1)
            self.assertIn("`source-a@v1#figure-1`", content)
            self.assertIn("![[assets/source-a/figure.png]]", content)
            self.assertIn("mmwiki:multimodal-evidence:start", content)
            self.assertEqual(refreshed_page["representation_layers"], ["text", "multimodal"])
            self.assertEqual(refreshed_page["last_ingest_stage"], "multimodal")
            self.assertEqual(refreshed_page["revision"], 1)

    def test_curate_removes_active_vault_data_but_preserves_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline = WikiPipeline(Path(directory))
            raw = pipeline.raw_root / "source-b/v1/manifest.json"
            raw.parent.mkdir(parents=True)
            raw.write_text("{}", encoding="utf-8")
            for relative in (
                "wiki/sources/source-a.md",
                "wiki/sources/source-b.md",
                "wiki/evidence/source-a-multimodal.md",
                "wiki/evidence/source-b-multimodal.md",
                "assets/source-b/figure.png",
            ):
                target = pipeline.vault / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("content", encoding="utf-8")
            pipeline._save_state(
                {
                    "schema_version": "0.3",
                    "sources": {
                        "source-a": {
                            "title": "A",
                            "source_version": "v1",
                            "wiki_path": "wiki/sources/source-a.md",
                            "evidence_map_path": "wiki/evidence/source-a-multimodal.md",
                            "items": [],
                            "assets": {},
                        },
                        "source-b": {
                            "title": "B",
                            "source_version": "v1",
                            "wiki_path": "wiki/sources/source-b.md",
                            "evidence_map_path": "wiki/evidence/source-b-multimodal.md",
                            "items": [],
                            "assets": {
                                "asset-b": {
                                    "vault_path": "assets/source-b/figure.png"
                                }
                            },
                        },
                    },
                    "pages": {},
                    "queries": [],
                }
            )
            pipeline.retrieval_index_path.write_text(
                json.dumps(
                    {
                        "sources": {"source-a": "v1", "source-b": "v1"},
                        "text": {
                            "records": [
                                {"source_id": "source-a"},
                                {"source_id": "source-b"},
                            ]
                        },
                        "visual": {"records": [{"source_id": "source-b"}]},
                    }
                ),
                encoding="utf-8",
            )

            preview = pipeline.curate_sources({"source-a"})
            self.assertEqual(preview["status"], "dry_run")
            self.assertTrue((pipeline.vault / "wiki/sources/source-b.md").is_file())

            result = pipeline.curate_sources({"source-a"}, apply=True)
            state = pipeline._load_state()
            index = json.loads(pipeline.retrieval_index_path.read_text())

            self.assertEqual(result["status"], "curated")
            self.assertEqual(set(state["sources"]), {"source-a"})
            self.assertFalse((pipeline.vault / "wiki/sources/source-b.md").exists())
            self.assertFalse((pipeline.vault / "assets/source-b").exists())
            self.assertTrue(raw.is_file())
            self.assertEqual(len(index["text"]["records"]), 1)
            self.assertEqual(index["visual"]["records"], [])

    def test_full_scale_analysis_merge_deduplicates_records_and_page_actions(self) -> None:
        merged = WikiPipeline._merge_wiki_analyses(
            [
                {
                    "summary": "第一页",
                    "claims": [{"statement": "事实", "evidence_refs": ["e1"]}],
                    "entities": [],
                    "concepts": [{"name": "概念"}],
                    "contradictions": [],
                    "page_actions": [
                        {
                            "title": "页面",
                            "kind": "analysis",
                            "action": "create",
                            "reason": "第一页证据",
                        }
                    ],
                    "_usage": {"total_tokens": 10},
                },
                {
                    "summary": "第二页",
                    "claims": [{"statement": "事实", "evidence_refs": ["e1"]}],
                    "entities": [],
                    "concepts": [{"name": "概念"}],
                    "contradictions": [],
                    "page_actions": [
                        {
                            "title": "页面",
                            "kind": "analysis",
                            "action": "update",
                            "reason": "第二页证据",
                        }
                    ],
                    "_usage": {"total_tokens": 12},
                },
            ]
        )

        self.assertEqual(merged["summary"], "第一页\n第二页")
        self.assertEqual(len(merged["claims"]), 1)
        self.assertEqual(len(merged["concepts"]), 1)
        self.assertEqual(len(merged["page_actions"]), 1)
        self.assertEqual(merged["page_actions"][0]["action"], "update")
        self.assertIn("第一页证据", merged["page_actions"][0]["reason"])
        self.assertIn("第二页证据", merged["page_actions"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
