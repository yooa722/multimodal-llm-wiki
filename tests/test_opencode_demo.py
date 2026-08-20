from __future__ import annotations

import re
import unittest
from pathlib import Path

from tools.opencode_demo import DEMO_CASES, markdown_table, render_live, render_live_result


class OpenCodeDemoTests(unittest.TestCase):
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

    def test_opencode_command_uses_deterministic_presenter_and_auto_mode(self) -> None:
        root = Path(__file__).parents[1]
        command = (root / ".opencode/commands/wiki-ask.md").read_text(
            encoding="utf-8"
        )
        agent = (root / ".opencode/agents/wiki-presenter.md").read_text(
            encoding="utf-8"
        )
        tool_source = (root / ".opencode/tools/wiki.ts").read_text(encoding="utf-8")
        passthrough = (
            root / ".opencode/plugins/wiki-result-passthrough.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("agent: wiki-presenter", command)
        visible_command = re.sub(r"<!--.*?-->", "", command, flags=re.S)
        self.assertIn("<!-- mmwiki-action", command)
        self.assertIn("tool: wiki_query", command)
        self.assertIn("mode: auto", command)
        self.assertNotIn("wiki_query", visible_command)
        self.assertNotIn("provider", visible_command)
        self.assertNotIn("默认使用 `hybrid`", command)
        self.assertIn("temperature: 0", agent)
        self.assertIn("隐藏的工具路由元数据", agent)
        self.assertIn("禁止添加开场白", agent)
        self.assertIn('markdownResult(context, "Wiki 完整回答"', tool_source)
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
                    "wiki_navigation": [],
                },
                "model": "test-model",
                "latency_ms": 12.3,
                "query_id": "query-test",
                "usage": {},
            }
        )
        self.assertIn("结论（最终回答）", rendered)
        self.assertIn("Wiki 定位", rendered)
        self.assertIn("原始 Evidence", rendered)
        self.assertIn("运行信息", rendered)
        self.assertIn("证据不足", rendered)

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

        self.assertIn("### Evidence 4", rendered)
        self.assertIn("第 4 条证据内容", rendered)
        self.assertIn("#item-4", rendered)

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
