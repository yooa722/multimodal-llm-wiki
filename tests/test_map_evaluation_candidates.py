import unittest

from tools.map_evaluation_candidates import candidate_summary, map_case_candidates


class MapEvaluationCandidatesTests(unittest.TestCase):
    def test_candidate_mapping_does_not_promote_unreviewed_labels(self):
        case = {
            "id": "q-1",
            "question": "预算是多少？",
            "expected_answer": "300 万元",
            "source_id": None,
            "evidence_item_ids": [],
            "annotation_status": "source_and_evidence_pending",
        }

        def search(query, top_k):
            self.assertIn("300 万元", query)
            self.assertEqual(top_k, 5)
            return {
                "hits": [
                    {
                        "source_id": "source-a",
                        "chunk_id": "chunk-a",
                        "item_ids": ["item-p0001-b0002"],
                        "pages": [1],
                        "modalities": ["table"],
                        "score": 12.3456789,
                        "title": "工期与预算",
                        "snippet": "开发测试 300 万元",
                        "wiki_paths": ["wiki/evidence/source-a-multimodal.md"],
                        "asset_paths": [],
                    }
                ],
                "retrieval": {"mode": "lexical", "channels": ["bm25"]},
            }

        mapped = map_case_candidates(case, search, top_k=5)

        self.assertIsNone(mapped["source_id"])
        self.assertEqual(mapped["evidence_item_ids"], [])
        self.assertEqual(mapped["candidate_sources"][0]["source_id"], "source-a")
        self.assertEqual(mapped["candidate_evidence"][0]["page_refs"], [1])
        self.assertEqual(
            mapped["annotation_status"],
            "candidate_generated_human_review_required",
        )
        self.assertTrue(mapped["candidate_retrieval"]["query_uses_expected_answer"])

    def test_summary_counts_missing_candidates(self):
        summary = candidate_summary(
            [
                {
                    "candidate_sources": [
                        {"source_id": "a", "modalities": ["image"]}
                    ]
                },
                {"candidate_sources": []},
            ]
        )
        self.assertEqual(summary["case_count"], 2)
        self.assertEqual(summary["cases_with_candidate"], 1)
        self.assertEqual(summary["cases_without_candidate"], 1)
        self.assertEqual(summary["top1_source_distribution"], {"a": 1})


if __name__ == "__main__":
    unittest.main()
