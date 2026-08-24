from __future__ import annotations

import unittest

from mmwiki.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    answer_requirements,
    normalize_math_markdown,
    parse_json_object,
    validate_answer_result,
    validate_wiki_analysis,
    validate_wiki_compilation,
)


class AnswerRequirementTests(unittest.TestCase):
    def test_requirements_adapt_to_question_shape_without_document_rules(self) -> None:
        value = answer_requirements(
            "请按顺序比较两种流程，并读取表格和图片。",
            [{"table": {"rows": [["a"]]}}],
            True,
        )

        self.assertIn("有序列表", value)
        self.assertIn("并列对照", value)
        self.assertIn("原始数值", value)
        self.assertIn("原图中可见", value)
        self.assertNotIn("Figure 4", value)
        self.assertNotIn("ReToken", value)

    def test_answer_injects_adaptive_requirements_into_model_input(self) -> None:
        provider = object.__new__(OpenAICompatibleProvider)
        captured = {}

        def fake_chat_json(system, user):
            captured["system"] = system
            captured["user"] = user
            return {
                "answer": "逐步回答",
                "answerable": True,
                "evidence_refs": ["source@v1#item-1"],
            }

        provider.chat_json = fake_chat_json
        provider.answer(
            "请按顺序说明流程",
            [{"id": "source@v1#item-1", "type": "paragraph"}],
            [],
        )

        prompt = captured["user"][0]["text"]
        self.assertIn("有序列表", prompt)
        self.assertIn("可直接展示给最终用户的完整 Markdown", prompt)
        self.assertIn("行内公式", prompt)
        self.assertIn("〔Evidence ID〕", prompt)


class MathMarkdownTests(unittest.TestCase):
    def test_normalizes_supported_inline_and_block_delimiters(self) -> None:
        value = normalize_math_markdown(
            "变量 $F$ 与 $L_1, \\dots, L_N$。\\[x^2 + y^2 = z^2\\]"
        )

        self.assertIn(r"\(F\)", value)
        self.assertIn(r"\(L_1, \dots, L_N\)", value)
        self.assertIn("\n$$\nx^2 + y^2 = z^2\n$$\n", value)

    def test_repairs_json_control_characters_inside_math(self) -> None:
        value = normalize_math_markdown(
            "$\x08ar{\\mathbf{v}}_1 + \x0crac{1}{2} + \theta + \rho$"
        )

        self.assertNotIn("\x08", value)
        self.assertNotIn("\x0c", value)
        self.assertNotIn("\t", value)
        self.assertNotIn("\r", value)
        self.assertIn(r"\bar{\mathbf{v}}_1", value)
        self.assertIn(r"\frac{1}{2}", value)
        self.assertIn(r"\theta", value)
        self.assertIn(r"\rho", value)

    def test_does_not_change_currency_or_code(self) -> None:
        value = normalize_math_markdown(
            "费用从 $100 到 $200，代码 `$F$`，公式 $F$。"
        )

        self.assertIn("$100", value)
        self.assertIn("$200", value)
        self.assertIn("`$F$`", value)
        self.assertIn(r"公式 \(F\)", value)


class JsonOutputTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(parse_json_object('{"answerable": false}'), {"answerable": False})

    def test_fenced_json(self) -> None:
        value = parse_json_object('```json\n{"answer": "证据不足"}\n```')
        self.assertEqual(value["answer"], "证据不足")

    def test_json_with_preamble_and_trailing_text(self) -> None:
        value = parse_json_object('结果如下：\n{"answerable": false}\n以上。')
        self.assertFalse(value["answerable"])

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(ProviderError):
            parse_json_object("not json")


class AnswerValidationTests(unittest.TestCase):
    def test_answerable_result_requires_candidate_evidence(self) -> None:
        with self.assertRaises(ProviderError):
            validate_answer_result(
                {"answer": "有答案", "answerable": True, "evidence_refs": []},
                {"source@v1#item-1"},
            )

    def test_answerable_must_be_real_boolean(self) -> None:
        with self.assertRaises(ProviderError):
            validate_answer_result(
                {
                    "answer": "证据不足",
                    "answerable": "false",
                    "evidence_refs": [],
                },
                set(),
            )

    def test_unanswerable_result_may_cite_checked_evidence(self) -> None:
        value = validate_answer_result(
            {
                "answer": "证据未提供精确数值",
                "answerable": False,
                "evidence_refs": ["source@v1#item-1"],
            },
            {"source@v1#item-1"},
        )
        self.assertFalse(value["answerable"])

    def test_answer_result_normalizes_math_before_display(self) -> None:
        value = validate_answer_result(
            {
                "answer": "$\x08ar{x}_1$",
                "answerable": True,
                "evidence_refs": ["source@v1#item-1"],
            },
            {"source@v1#item-1"},
        )

        self.assertEqual(value["answer"], r"\(\bar{x}_1\)")


class WikiValidationTests(unittest.TestCase):
    def test_analysis_normalizes_entity_concept_and_contradiction_shorthand(self) -> None:
        value = validate_wiki_analysis(
            {
                "summary": "摘要",
                "claims": [],
                "entities": ["ReToken"],
                "concepts": ["视觉检索"],
                "contradictions": ["图文数值不一致"],
                "page_actions": [],
            },
            set(),
        )

        self.assertEqual(value["entities"], [{"name": "ReToken"}])
        self.assertEqual(value["concepts"], [{"name": "视觉检索"}])
        self.assertEqual(
            value["contradictions"], [{"statement": "图文数值不一致"}]
        )

    def test_analysis_rejects_invalid_page_action(self) -> None:
        with self.assertRaisesRegex(ProviderError, "kind"):
            validate_wiki_analysis(
                {
                    "summary": "摘要",
                    "claims": [],
                    "entities": [],
                    "concepts": [],
                    "contradictions": [],
                    "page_actions": [
                        {
                            "title": "页面",
                            "kind": "source",
                            "action": "create",
                            "reason": "测试",
                        }
                    ],
                },
                set(),
            )

    def test_compilation_requires_evidence_for_every_page(self) -> None:
        with self.assertRaisesRegex(ProviderError, "evidence_refs"):
            validate_wiki_compilation(
                {
                    "summary": "摘要",
                    "pages": [
                        {
                            "title": "页面",
                            "kind": "concept",
                            "summary": "摘要",
                            "content": "正文",
                            "evidence_refs": [],
                        }
                    ],
                },
                {"source@v1#item-1"},
            )

    def test_compilation_normalizes_valid_page(self) -> None:
        value = validate_wiki_compilation(
            {
                "summary": "摘要",
                "pages": [
                    {
                        "title": " 页面 ",
                        "kind": "analysis",
                        "summary": "摘要",
                        "content": "正文",
                        "evidence_refs": ["source@v1#item-1", "source@v1#item-1"],
                    }
                ],
            },
            {"source@v1#item-1"},
        )

        self.assertEqual(value["pages"][0]["title"], "页面")
        self.assertEqual(value["pages"][0]["evidence_refs"], ["source@v1#item-1"])

    def test_compile_accepts_preserved_existing_evidence_refs(self) -> None:
        provider = object.__new__(OpenAICompatibleProvider)
        captured = {}

        def fake_chat_json(system, user):
            captured["user"] = user
            return {
                "summary": "摘要",
                "pages": [
                    {
                        "title": "页面",
                        "kind": "concept",
                        "summary": "摘要",
                        "content": "保留旧文本并补充图片事实",
                        "evidence_refs": [
                            "source@v1#text-1",
                            "source@v1#image-1",
                            "source@v1#text-1",
                        ],
                    }
                ],
            }

        provider.chat_json = fake_chat_json
        value = provider.compile_wiki(
            "来源",
            {"page_actions": [{"title": "页面", "kind": "concept"}]},
            [{"id": "source@v1#image-1"}],
            [
                {
                    "title": "页面",
                    "path": "wiki/concepts/Page.md",
                    "content": "旧正文",
                    "evidence_ids": ["source@v1#text-1"],
                }
            ],
            "schema",
            preserved_evidence_ids={"source@v1#text-1"},
            stage="multimodal",
        )

        self.assertEqual(
            value["pages"][0]["evidence_refs"],
            ["source@v1#text-1", "source@v1#image-1"],
        )
        self.assertIn("已有页面中的合法 evidence_refs 可以保留", captured["user"])

    def test_compile_rejects_evidence_refs_outside_current_and_preserved(self) -> None:
        provider = object.__new__(OpenAICompatibleProvider)

        def fake_chat_json(system, user):
            return {
                "summary": "摘要",
                "pages": [
                    {
                        "title": "页面",
                        "kind": "concept",
                        "summary": "摘要",
                        "content": "正文",
                        "evidence_refs": ["source@v1#unknown"],
                    }
                ],
            }

        provider.chat_json = fake_chat_json
        with self.assertRaisesRegex(ProviderError, "evidence_refs"):
            provider.compile_wiki(
                "来源",
                {"page_actions": [{"title": "页面", "kind": "concept"}]},
                [{"id": "source@v1#image-1"}],
                [],
                "schema",
                preserved_evidence_ids={"source@v1#text-1"},
                stage="multimodal",
            )

    def test_visual_wiki_analysis_receives_actual_image_with_evidence_identity(self) -> None:
        provider = object.__new__(OpenAICompatibleProvider)
        captured = {}

        def fake_chat_json(system, user):
            captured["system"] = system
            captured["user"] = user
            return {
                "summary": "摘要",
                "claims": [
                    {
                        "statement": "图中存在箭头关系",
                        "evidence_refs": ["source@v1#image-1"],
                        "provenance": "inferred",
                    }
                ],
                "entities": [],
                "concepts": [],
                "contradictions": [],
                "page_actions": [],
            }

        provider.chat_json = fake_chat_json
        value = provider.analyze_wiki(
            "来源",
            [{"id": "source@v1#image-1", "type": "image"}],
            [],
            "schema",
            [
                {
                    "evidence_id": "source@v1#image-1",
                    "data_url": "data:image/png;base64,AAAA",
                }
            ],
        )

        self.assertEqual(value["claims"][0]["provenance"], "inferred")
        self.assertIsInstance(captured["user"], list)
        self.assertTrue(
            any(part.get("type") == "image_url" for part in captured["user"])
        )
        self.assertTrue(
            any(
                "source@v1#image-1" in part.get("text", "")
                for part in captured["user"]
                if part.get("type") == "text"
            )
        )

    def test_text_only_wiki_analysis_discards_ungrounded_image_annotations(self) -> None:
        provider = object.__new__(OpenAICompatibleProvider)

        def fake_chat_json(system, user):
            return {
                "summary": "摘要",
                "claims": [],
                "entities": [],
                "concepts": [],
                "contradictions": [],
                "page_actions": [],
                "image_annotations": [
                    {
                        "asset_id": "asset-not-supplied",
                        "evidence_id": "source@v1#image-1",
                        "caption": "未读取像素的注释",
                    }
                ],
            }

        provider.chat_json = fake_chat_json
        value = provider.analyze_wiki(
            "来源",
            [{"id": "source@v1#image-1", "type": "image"}],
            [],
            "schema",
            [],
        )

        self.assertEqual(value["image_annotations"], [])


if __name__ == "__main__":
    unittest.main()
