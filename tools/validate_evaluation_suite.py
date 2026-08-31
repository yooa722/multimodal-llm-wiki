from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="校验多模态 Wiki 评测金标")
    parser.add_argument("suite", type=Path)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=PROJECT_ROOT / "runtime",
        help="待校验 Runtime；默认使用项目 runtime/",
    )
    args = parser.parse_args()
    state = json.loads((args.runtime_root / "state.json").read_text(encoding="utf-8"))
    cases = load_jsonl(args.suite)
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    sources = state.get("sources", {})
    pages = state.get("pages", {})
    for index, case in enumerate(cases, 1):
        case_id = str(case.get("id") or "")
        question = str(case.get("question") or "").strip()
        source_id = str(case.get("source_id") or "")
        prefix = f"第 {index} 行/{case_id or '无 ID'}"
        if not case_id or case_id in seen_ids:
            errors.append(f"{prefix}: ID 为空或重复")
        seen_ids.add(case_id)
        normalized_question = question.casefold()
        if not question or normalized_question in seen_questions:
            errors.append(f"{prefix}: 问题为空或重复")
        seen_questions.add(normalized_question)
        if source_id not in sources:
            errors.append(f"{prefix}: 未知来源 {source_id}")
            continue
        known_items = {
            str(item.get("item_id") or "") for item in sources[source_id].get("items", [])
        }
        evidence_ids = case.get("evidence_item_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            errors.append(f"{prefix}: evidence_item_ids 必须非空")
        else:
            unknown_items = set(map(str, evidence_ids)) - known_items
            if unknown_items:
                errors.append(f"{prefix}: 未知 Evidence {sorted(unknown_items)}")
        answerable = case.get("expected_answerable")
        if not isinstance(answerable, bool):
            errors.append(f"{prefix}: expected_answerable 必须为布尔值")
        if answerable and not str(case.get("expected_answer") or "").strip():
            errors.append(f"{prefix}: 可回答题缺少 expected_answer")
        groups = case.get("required_keyword_groups", [])
        if answerable and (not isinstance(groups, list) or not groups):
            errors.append(f"{prefix}: 可回答题缺少 required_keyword_groups")
        for path in case.get("wiki_page_paths", []):
            page = pages.get(path)
            if page is None:
                errors.append(f"{prefix}: 未知 Wiki 金标页 {path}")
            elif source_id not in page.get("source_ids", []):
                errors.append(f"{prefix}: Wiki 金标页不覆盖来源 {path}")

    summary = {
        "status": "failed" if errors else "passed",
        "cases": len(cases),
        "sources": dict(Counter(str(case.get("source_id")) for case in cases)),
        "modalities": dict(Counter(str(case.get("modality")) for case in cases)),
        "difficulties": dict(Counter(str(case.get("difficulty")) for case in cases)),
        "answerable": dict(
            Counter(str(case.get("expected_answerable")) for case in cases)
        ),
        "wiki_page_gold_cases": sum(bool(case.get("wiki_page_paths")) for case in cases),
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
