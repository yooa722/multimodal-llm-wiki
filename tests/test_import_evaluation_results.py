import json
import tempfile
import unittest
from pathlib import Path

from tools.import_evaluation_results import baseline_summary, rows_to_cases, write_jsonl


class ImportEvaluationResultsTests(unittest.TestCase):
    def test_rows_are_converted_to_pending_cases(self):
        rows = [
            ["数据集ID", "会话ID", "问题ID", "问题", "标准回答", "RAG生成答案"],
            ["dataset-a", "session-a", "question-a", "问题？", "答案", "较长的旧答案"],
        ]

        cases = rows_to_cases(rows)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["id"], "question-a")
        self.assertIsNone(cases[0]["source_id"])
        self.assertEqual(cases[0]["evidence_item_ids"], [])
        self.assertEqual(
            cases[0]["annotation_status"], "source_and_evidence_pending"
        )

    def test_incomplete_row_is_rejected(self):
        rows = [
            ["数据集ID", "会话ID", "问题ID", "问题", "标准回答", "RAG生成答案"],
            ["dataset-a", "session-a", "question-a", "", "答案", "旧答案"],
        ]
        with self.assertRaisesRegex(ValueError, "问题"):
            rows_to_cases(rows)

    def test_baseline_summary_and_jsonl(self):
        rows = [
            ["数据集ID", "会话ID", "问题ID", "问题", "标准回答", "RAG生成答案"],
            ["d", "s", "q", "问题？", "300万元", "预算是300万元〔1〕"],
        ]
        cases = rows_to_cases(rows)
        summary = baseline_summary(cases)
        self.assertEqual(summary["case_count"], 1)
        self.assertEqual(summary["gold_contained_in_legacy_count"], 1)
        self.assertEqual(summary["legacy_with_citation_marker_count"], 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cases.jsonl"
            write_jsonl(cases, output)
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(loaded["question"], "问题？")


if __name__ == "__main__":
    unittest.main()
