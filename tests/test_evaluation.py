from __future__ import annotations

import unittest

from tools.evaluate_retrieval import (
    first_navigation_rank,
    first_relevant_rank,
    first_wiki_page_rank,
)
from tools.evaluate_online import concept_coverage_pass, keyword_groups


class EvaluationIdentityTests(unittest.TestCase):
    def test_relevance_requires_source_and_item_identity(self) -> None:
        case = {"source_id": "source-a", "evidence_item_ids": ["item-1"]}
        hits = [
            {"source_id": "source-b", "item_ids": ["item-1"]},
            {"source_id": "source-a", "item_ids": ["item-1"]},
        ]

        self.assertEqual(first_relevant_rank(hits, case), 2)

    def test_navigation_recall_uses_page_source_ids(self) -> None:
        trace = {
            "wiki_navigation": [
                {"source_ids": ["source-b"]},
                {"source_ids": ["source-a", "source-c"]},
            ]
        }

        self.assertEqual(first_navigation_rank(trace, "source-a"), 2)
        self.assertIsNone(first_navigation_rank(trace, "missing"))

    def test_semantic_navigation_takes_precedence_when_available(self) -> None:
        trace = {
            "wiki_navigation_sources": [
                {"source_id": "source-a", "rank": 1}
            ],
            "wiki_navigation": [
                {"source_ids": ["source-b"]},
                {"source_ids": ["source-a"]},
            ],
        }

        self.assertEqual(first_navigation_rank(trace, "source-a"), 1)

    def test_wiki_page_recall_uses_explicit_gold_paths(self) -> None:
        trace = {
            "wiki_navigation": [
                {"path": "wiki/concepts/other.md"},
                {"path": "wiki/concepts/target.md"},
            ]
        }
        case = {"wiki_page_paths": ["wiki/concepts/target.md"]}

        self.assertEqual(first_wiki_page_rank(trace, case), 2)
        self.assertIsNone(first_wiki_page_rank(trace, {}))

    def test_declared_keyword_aliases_allow_cross_language_equivalence(self) -> None:
        case = {
            "required_keywords": ["答案"],
            "required_keyword_groups": [["答案", "answer"]],
        }

        self.assertEqual(keyword_groups(case), [["答案", "answer"]])
        self.assertTrue(concept_coverage_pass("The Answer is generated.", case))

    def test_concept_groups_default_to_strict_keywords(self) -> None:
        case = {"required_keywords": ["Top-K", "缓存"]}

        self.assertTrue(concept_coverage_pass("Top-K 帧进入缓存", case))
        self.assertFalse(concept_coverage_pass("Top-K frames are selected", case))


if __name__ == "__main__":
    unittest.main()
