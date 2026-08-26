from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.opencode_demo import (
    DEMO_CASES,
    _derived_visual_text,
    markdown_table,
    render_live,
    render_live_result,
)


class OpenCodeDemoTests(unittest.TestCase):
    def test_visual_status_text_distinguishes_policy_skip_from_missing_build(self) -> None:
        self.assertEqual(_derived_visual_text({}), "尚未构建")
        self.assertEqual(
            _derived_visual_text(
                {
                    "status": "skipped",
                    "processing_policy": {
                        "reason": "自然图片优先生成 VLM Caption，OCR 保留为按需能力"
                    },
                }
            ),
            "按处理策略未执行：自然图片优先生成 VLM Caption，OCR 保留为按需能力",
        )

    def test_auto_mode_reaches_pipeline_without_command_side_rerouting(self) -> None:
        rendered = render_live(
            object(),
            "Figure 4 中箭头的方向是什么",
            "auto",
            "baseline",
            True,
        )
        self.assertIn("检索模式：`auto`", rendered)
        self.assertTrue(all(case["mode"] == "auto" for case in DEMO_CASES.values()))

    def test_opencode_command_hides_routing_metadata_from_user_bubble(self) -> None:
        root = Path(__file__).parents[1]
        command_dir = root / ".opencode/commands"
        commands = {
            path.name: path.read_text(encoding="utf-8")
            for path in command_dir.glob("wiki-*.md")
        }
        command = commands["wiki-ask.md"]
        agent = (
            root / ".opencode/agents/wiki-query-presenter.md"
        ).read_text(encoding="utf-8")
        presenter = (root / ".opencode/agents/wiki-presenter.md").read_text(
            encoding="utf-8"
        )
        tool_source = (root / ".opencode/tools/wiki.ts").read_text(encoding="utf-8")
        passthrough = (
            root / ".opencode/plugins/wiki-result-passthrough.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("agent: wiki-query-presenter", command)
        command_body = command.split("---", 2)[-1].strip()
        self.assertEqual(command_body, "$ARGUMENTS")
        self.assertNotIn("mmwiki-action", command)
        self.assertNotIn("wiki_query", command)
        self.assertNotIn("provider", command_body)
        for name, source in commands.items():
            body = source.split("---", 2)[-1]
            self.assertNotIn("mmwiki-action", source, name)
            self.assertNotIn("tool: wiki_", body, name)
            self.assertNotIn("provider:", body, name)
            self.assertNotIn("mode:", body, name)
        self.assertIn("`wiki_query`", agent)
        self.assertIn("`mode=auto`", agent)
        self.assertIn("`provider=api`", agent)
        self.assertIn("完整问题原样放入 `question`", agent)
        self.assertIn("不得改用 Bash", agent)
        self.assertIn('"*": deny', agent)
        self.assertIn("wiki_query: allow", agent)
        self.assertIn("temperature: 0", agent)
        self.assertIn("不包含需要向用户解释的路由指令", agent)
        self.assertIn("禁止添加开场白", agent)
        self.assertIn("不包含工具名、参数或隐藏路由标记", presenter)
        self.assertIn("wiki_status: allow", presenter)
        self.assertIn("wiki_query: allow", presenter)
        self.assertIn('"*": deny', presenter)
        self.assertNotIn("mmwiki-action", presenter)
        self.assertIn("不得调用 Bash", presenter)
        self.assertIn('markdownResult(context, "Wiki 完整回答"', tool_source)
        self.assertIn('PRESENTATION_VERSION = "split-query-v2"', tool_source)
        self.assertIn('"tool.execute.after"', passthrough)
        self.assertIn('"experimental.text.complete"', passthrough)
        self.assertIn("output.text = wikiOutput", passthrough)
        self.assertIn("pendingBySession.delete", passthrough)

    def test_markdown_table_keeps_complete_rows_and_escapes_pipes(self) -> None:
        rendered = markdown_table(
            {"rows": [["阶段", "预算"], ["开发|测试", "300"]]}
        )
        self.assertIn("| 阶段 | 预算 |", rendered)
        self.assertIn("开发\\|测试", rendered)
        self.assertIn("300", rendered)

    def test_live_result_exposes_required_presentation_sections(self) -> None:
        rendered = render_live_result(
            {
                "question": "测试问题",
                "answer": "证据不足，无法回答。",
                "citations": [],
                "retrieval": {
                    "requested_mode": "multimodal",
                    "mode": "multimodal",
                    "routing_reason": "检测到箭头、流程或连接，进入 Multimodal",
                    "mode_transition": ["hybrid", "multimodal"],
                    "fallback_reason": None,
                    "wiki_navigation": [],
                },
                "model": "test-model",
                "latency_ms": 12.3,
                "query_id": "query-test",
                "usage": {},
            }
        )
        self.assertIn("## 结论", rendered)
        self.assertIn("## 知识入口", rendered)
        self.assertIn("## 证据依据", rendered)
        self.assertIn("运行信息", rendered)
        self.assertNotIn("## 引用索引", rendered)
        self.assertIn("证据不足", rendered)
        self.assertIn("请求 `multimodal` / 实际 `multimodal`", rendered)
        self.assertIn("路由依据", rendered)
        self.assertIn("hybrid → multimodal", rendered)
        self.assertIn("| 回退 | 无 |", rendered)

    def test_live_result_does_not_drop_later_cited_evidence(self) -> None:
        citations = [
            {
                "source_id": "source",
                "chunk_id": f"chunk-{index}",
                "title": f"证据 {index}",
                "snippet": f"第 {index} 条证据内容",
                "item_ids": [f"item-{index}"],
                "evidence_ids": [f"source@v1#item-{index}"],
                "modalities": ["paragraph"],
                "path": "wiki/sources/source.md",
            }
            for index in range(1, 5)
        ]
        rendered = render_live_result(
            {
                "question": "比较并解释",
                "answer": "完整回答",
                "citations": citations,
                "retrieval": {"requested_mode": "hybrid", "mode": "hybrid"},
                "model": "test-model",
                "usage": {},
            }
        )

        self.assertIn("### 〔4〕", rendered)
        self.assertIn("第 4 条证据内容", rendered)
        self.assertIn("#item-4", rendered)

    def test_live_result_maps_inline_ids_to_numbered_evidence_cards(self) -> None:
        evidence_id = "source@v1#item-1"
        state = {
            "sources": {
                "source": {
                    "title": "source",
                    "items": [
                        {
                            "item_id": "item-1",
                            "item_type": "table",
                            "page_start": 1,
                            "page_end": 1,
                            "table": {
                                "rows": [["阶段", "预算"], ["开发测试", "300"]]
                            },
                            "asset_ids": ["asset-1"],
                        }
                    ],
                    "assets": {
                        "asset-1": {"vault_path": "assets/source/table.png"}
                    },
                }
            },
            "pages": {
                "wiki/entities/项目方案.md": {
                    "path": "wiki/entities/项目方案.md",
                    "title": "智慧交通管理平台建设方案",
                    "kind": "entity",
                    "source_ids": ["source"],
                    "evidence_ids": [evidence_id],
                }
            },
        }
        result = {
            "question": "预算是多少",
            "answer": f"开发测试阶段预算为300万元〔{evidence_id}〕。",
            "evidence_refs": [evidence_id],
            "evidence_locations": [
                {
                    "evidence_id": evidence_id,
                    "page_index": 0,
                    "page_number": 1,
                    "block_index": 10,
                    "paragraph_index": None,
                    "location_label": "第 1 页 · 表格区域",
                    "breadcrumb": "项目实施 > 工期与预算",
                    "raw_ref": "content_list_v2[0][9]",
                    "quote": "开发测试阶段工期120天，人力15人，预算300万元。",
                    "bbox": {"values": [100, 200, 900, 700]},
                }
            ],
            "citations": [
                {
                    "source_id": "source",
                    "chunk_id": "chunk-1",
                    "title": "图片派生证据",
                    "snippet": "开发测试 300",
                    "item_ids": ["item-1"],
                    "evidence_ids": [evidence_id],
                    "modalities": ["table"],
                    "pages": [1],
                    "path": "wiki/sources/source.md",
                    "wiki_paths": ["wiki/entities/项目方案.md"],
                    "asset_paths": ["assets/source/table.png"],
                }
            ],
            "retrieval": {
                "requested_mode": "hybrid",
                "mode": "hybrid",
                "wiki_navigation": [
                    {
                        "title": "智慧交通管理平台建设方案",
                        "path": "wiki/entities/项目方案.md",
                        "navigation_channels": ["page_bm25", "page_embedding"],
                        "summary": "这里是一段不应展示给终端用户的内部检索摘要。",
                    },
                    {
                        "title": "智慧交通项目评审分析",
                        "path": "wiki/topics/项目评审.md",
                        "navigation_channels": ["page_embedding"],
                        "summary": "第二段内部检索摘要。",
                    },
                    {
                        "title": "不应展示的第三个入口",
                        "path": "wiki/topics/第三页.md",
                        "navigation_channels": ["page_bm25"],
                        "summary": "第三段内部检索摘要。",
                    },
                ],
            },
            "model": "test-model",
            "usage": {},
            "query_id": "query-test",
        }

        fake_pipeline = Mock()
        fake_pipeline._load_state.return_value = state
        with patch("tools.opencode_demo.WikiPipeline", return_value=fake_pipeline):
            rendered = render_live_result(result)

        self.assertIn(
            "300万元〔1〕",
            rendered,
        )
        self.assertNotIn("## 引用索引", rendered)
        self.assertIn("### 〔1〕 工期与预算", rendered)
        self.assertIn("- **来源：** 智慧交通管理平台建设方案", rendered)
        self.assertIn("- **位置：** 第 1 页 · 表格区域", rendered)
        self.assertIn("- **类型：** 表格", rendered)
        self.assertIn("- **章节：** 项目实施 > 工期与预算", rendered)
        self.assertIn(f"**Evidence ID：** `{evidence_id}`", rendered)
        self.assertIn("[查看 Wiki 页面]", rendered)
        self.assertIn("[定位原始 Evidence]", rendered)
        self.assertIn("[打开原图]", rendered)
        self.assertIn("wiki/sources/source.md#item-1", rendered)
        self.assertNotIn("/query/view", rendered)
        self.assertNotIn("[查看完整 Wiki 页面]", rendered)
        self.assertNotIn("[浏览器深度核验]", rendered)
        self.assertNotIn("[浏览器打开原图]", rendered)
        self.assertIn("智慧交通管理平台建设方案", rendered)
        self.assertIn("智慧交通项目评审分析", rendered)
        self.assertNotIn("不应展示的第三个入口", rendered)
        self.assertNotIn("page_bm25", rendered)
        self.assertNotIn("page_embedding", rendered)
        self.assertNotIn("内部检索摘要", rendered)
        conclusion = rendered.split("## 知识入口", 1)[0]
        self.assertNotIn("/query/view", conclusion)
        self.assertIn("| 开发测试 | 300 |", rendered)

    def test_image_evidence_shows_preview_and_three_labeled_descriptions(self) -> None:
        evidence_id = "paper@v1#item-image"
        state = {
            "sources": {
                "paper": {
                    "title": "ReToken 论文",
                    "items": [
                        {
                            "item_id": "item-image",
                            "item_type": "image",
                            "page_start": 6,
                            "page_end": 6,
                            "caption": "Figure 4: ReToken inference pipeline.",
                            "asset_ids": ["asset-figure-4"],
                        }
                    ],
                    "assets": {
                        "asset-figure-4": {
                            "vault_path": "assets/paper/figure-4.png"
                        }
                    },
                    "visual_evidence": [
                        {
                            "asset_id": "asset-figure-4",
                            "kind": "image_ocr",
                            "text": "visual tokens question tokens",
                            "provenance": {
                                "source": "qwen3.5-ocr",
                                "model": "qwen3.5-ocr",
                                "task": "text_recognition",
                            },
                        },
                        {
                            "asset_id": "asset-figure-4",
                            "kind": "image_caption",
                            "text": "图中展示两阶段推理流程。",
                            "provenance": {
                                "source": "vlm",
                                "model": "qwen3-vl-plus",
                            },
                        },
                    ],
                }
            },
            "pages": {},
        }
        result = {
            "question": "Figure 4 展示了什么",
            "answer": "图中展示两阶段推理流程。",
            "evidence_refs": [evidence_id],
            "citations": [
                {
                    "source_id": "paper",
                    "chunk_id": "chunk-image",
                    "title": "Figure 4",
                    "snippet": "ReToken inference pipeline",
                    "item_ids": ["item-image"],
                    "evidence_ids": [evidence_id],
                    "modalities": ["image"],
                    "pages": [6],
                    "path": "wiki/sources/paper.md",
                    "asset_paths": ["assets/paper/figure-4.png"],
                    "matched_asset_id": "asset-figure-4",
                    "matched_asset_path": "assets/paper/figure-4.png",
                }
            ],
            "retrieval": {"requested_mode": "multimodal", "mode": "multimodal"},
            "model": "test-model",
            "usage": {},
        }

        fake_pipeline = Mock()
        fake_pipeline._load_state.return_value = state
        with patch("tools.opencode_demo.WikiPipeline", return_value=fake_pipeline):
            rendered = render_live_result(result)

        self.assertIn("#### 图片解析", rendered)
        self.assertIn(
            "![〔1〕 Figure 4 原图](http://127.0.0.1:19828/api/v1/media/",
            rendered,
        )
        self.assertIn("[打开原图]", rendered)
        self.assertIn("**原始 Caption**", rendered)
        self.assertIn("Figure 4: ReToken inference pipeline.", rendered)
        self.assertIn("**OCR 文字**", rendered)
        self.assertIn("qwen3.5-ocr · text_recognition", rendered)
        self.assertIn("visual tokens question tokens", rendered)
        self.assertIn("**VLM 理解**", rendered)
        self.assertIn("qwen3-vl-plus", rendered)
        self.assertIn("图中展示两阶段推理流程。", rendered)

    def test_live_result_normalizes_historical_answer_math(self) -> None:
        rendered = render_live_result(
            {
                "question": "公式是什么",
                "answer": "向量为 $\x08ar{\\mathbf{v}}_1$。",
                "citations": [],
                "retrieval": {"requested_mode": "hybrid", "mode": "hybrid"},
                "model": "test-model",
                "usage": {},
            }
        )

        self.assertNotIn("\x08", rendered)
        self.assertIn(r"\(\bar{\mathbf{v}}_1\)", rendered)


if __name__ == "__main__":
    unittest.main()
