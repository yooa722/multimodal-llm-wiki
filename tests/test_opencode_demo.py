from __future__ import annotations

import unittest

from tools.opencode_demo import choose_mode, markdown_table, render_live_result


class OpenCodeDemoTests(unittest.TestCase):
    def test_choose_mode_uses_multimodal_only_for_visual_details(self) -> None:
        self.assertEqual(choose_mode("开发测试阶段预算是多少"), "hybrid")
        self.assertEqual(choose_mode("Figure 4 中箭头的方向是什么"), "multimodal")
        self.assertEqual(choose_mode("图中蓝色曲线代表什么"), "multimodal")

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
