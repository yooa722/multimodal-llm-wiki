from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mmwiki.config import (
    FeatureConfig,
    detect_visual_intent,
    load_feature_config,
    resolve_query_mode,
    resolve_visual_processing_policy,
)


class FeatureConfigTests(unittest.TestCase):
    def test_defaults_disable_vlm_and_vector_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                config = load_feature_config(Path(directory))
        self.assertEqual(config, FeatureConfig(enable_vlm=False, enable_vector_retrieval=False))

    def test_cli_overrides_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "MMWIKI_ENABLE_VLM=false\nMMWIKI_ENABLE_VECTOR_RETRIEVAL=false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_feature_config(root, vlm="on", vector_retrieval="on")
        self.assertTrue(config.enable_vlm)
        self.assertTrue(config.enable_vector_retrieval)

    def test_auto_mode_uses_caption_lexical_search_by_default(self) -> None:
        config = FeatureConfig()
        self.assertEqual(resolve_query_mode("auto", False, config), "lexical")
        self.assertEqual(resolve_query_mode("multimodal", True, config), "lexical")

    def test_auto_mode_enables_hybrid_or_multimodal_only_when_configured(self) -> None:
        vectors = FeatureConfig(enable_vector_retrieval=True)
        self.assertEqual(resolve_query_mode("auto", False, vectors), "hybrid")
        self.assertEqual(
            resolve_query_mode(
                "auto", True, FeatureConfig(enable_vlm=True, enable_vector_retrieval=True)
            ),
            "multimodal",
        )

    def test_visual_intent_covers_pixel_questions_without_misrouting_facts(self) -> None:
        config = FeatureConfig(enable_vlm=True, enable_vector_retrieval=True)
        visual_questions = (
            "蓝色曲线表示什么？",
            "左侧物体是什么？",
            "箭头指向哪个模块？",
            "图中有几个人？",
            "图中的人物和趋势分别是什么？",
        )
        for question in visual_questions:
            intent = detect_visual_intent(question)
            self.assertTrue(intent.is_visual, question)
            self.assertEqual(
                resolve_query_mode("auto", intent.is_visual, config),
                "multimodal",
            )

        factual_questions = (
            "开发测试阶段预算是多少？",
            "请读取工期预算表中的人力和预算。",
            "图数据库包含多少节点？",
            "公司总部位于北京哪个区域？",
            "开发预算为什么呈上升趋势？",
        )
        for question in factual_questions:
            intent = detect_visual_intent(question)
            self.assertFalse(intent.is_visual, question)
            self.assertEqual(
                resolve_query_mode("auto", intent.is_visual, config),
                "hybrid",
            )

    def test_visual_processing_policy_is_cost_aware(self) -> None:
        natural = resolve_visual_processing_policy(item_type="image")
        self.assertTrue(natural.run_caption)
        self.assertFalse(natural.run_ocr)
        self.assertEqual(natural.ocr_policy, "on_demand")

        chart = resolve_visual_processing_policy(item_type="chart")
        self.assertTrue(chart.run_caption)
        self.assertTrue(chart.run_ocr)

        table = resolve_visual_processing_policy(
            item_type="table", has_structured_table=True
        )
        self.assertEqual(table.primary_representation, "structured_table")
        self.assertFalse(table.run_caption)
        self.assertTrue(table.run_ocr)

        table_screenshot = resolve_visual_processing_policy(
            item_type="image", caption="A table screenshot"
        )
        self.assertEqual(table_screenshot.resource_type, "table_screenshot")
        self.assertFalse(table_screenshot.run_caption)
        self.assertTrue(table_screenshot.run_ocr)

        page_screenshot = resolve_visual_processing_policy(
            item_type="image", metadata={"visual_type": "page_screenshot"}
        )
        self.assertEqual(page_screenshot.resource_type, "page_screenshot")
        self.assertTrue(page_screenshot.run_caption)
        self.assertTrue(page_screenshot.run_ocr)

        formula = resolve_visual_processing_policy(
            item_type="equation", has_latex=True
        )
        self.assertEqual(formula.primary_representation, "latex")
        self.assertFalse(formula.run_caption)
        self.assertFalse(formula.run_ocr)


if __name__ == "__main__":
    unittest.main()
