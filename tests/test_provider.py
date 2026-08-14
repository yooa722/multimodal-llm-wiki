from __future__ import annotations

import unittest

from mmwiki.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    parse_json_object,
    validate_answer_result,
    validate_wiki_analysis,
    validate_wiki_compilation,
)


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


if __name__ == "__main__":
    unittest.main()
