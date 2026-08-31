from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.pipeline import WikiPipeline
from mmwiki.retrieval import RETRIEVAL_MODES


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values: list[float], ratio: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * ratio) - 1)]


def keyword_groups(case: dict[str, Any]) -> list[list[str]]:
    """Return declared concept aliases, falling back to strict single-keyword groups."""
    groups = case.get("required_keyword_groups")
    if groups is None:
        return [[str(keyword)] for keyword in case.get("required_keywords", [])]
    if not isinstance(groups, list) or any(
        not isinstance(group, list)
        or not group
        or any(not isinstance(alias, str) or not alias.strip() for alias in group)
        for group in groups
    ):
        raise ValueError("required_keyword_groups 必须是非空字符串数组组成的数组")
    return groups


def concept_coverage_pass(answer: str, case: dict[str, Any]) -> bool:
    normalized = answer.casefold()
    return all(
        any(alias.casefold() in normalized for alias in group)
        for group in keyword_groups(case)
    )


def grouped_metrics(
    rows: list[dict[str, Any]], cases: list[dict[str, Any]], field: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    values = sorted({str(case.get(field) or "unknown") for case in cases})
    for value in values:
        pairs = [
            (row, case)
            for row, case in zip(rows, cases)
            if str(case.get(field) or "unknown") == value
        ]
        answerable = [row for row, case in pairs if case["expected_answerable"]]
        result[value] = {
            "cases": len(pairs),
            "answerability_accuracy": round(
                sum(row["answerability_correct"] for row, _ in pairs) / len(pairs),
                4,
            ),
            "concept_coverage_accuracy": round(
                sum(row["concept_coverage_pass"] for row in answerable)
                / len(answerable),
                4,
            ) if answerable else None,
            "citation_accuracy": round(
                sum(row["citation_hit"] for row, _ in pairs) / len(pairs), 4
            ),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="运行在线多模态问答冒烟评测")
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("evaluation/official_image_text_10_verified.jsonl"),
    )
    parser.add_argument("--output", type=Path, default=Path("reports/online-smoke-results.json"))
    parser.add_argument(
        "--retrieval-mode", choices=("auto", *RETRIEVAL_MODES), default="hybrid"
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--scope",
        choices=("corpus", "source"),
        default="corpus",
        help="corpus 为全库问答；source 仅用于已知来源内诊断",
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="允许检索模式回退；默认回退会使本次评测失败",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="待评测项目根目录；默认使用当前仓库",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="可选：使用隔离 Runtime，避免覆盖默认演示知识库",
    )
    parser.add_argument(
        "--provider",
        choices=("api", "baseline"),
        default="api",
        help="api 用于正式在线评测；baseline 只用于本地链路诊断",
    )
    args = parser.parse_args()
    pipeline = WikiPipeline(args.root, runtime_root=args.runtime_root)
    cases = load_jsonl(args.suite)
    rows: list[dict[str, Any]] = []
    for case in cases:
        try:
            result = pipeline.query(
                case["question"],
                top_k=args.top_k,
                provider=args.provider,
                source_ids=(
                    {case["source_id"]} if args.scope == "source" else None
                ),
                retrieval_mode=args.retrieval_mode,
            )
        except Exception as exc:  # 将 API 波动记录为失败，继续执行剩余题目
            rows.append(
                {
                    "id": case["id"],
                    "source_id": case["source_id"],
                    "modality": case["modality"],
                    "difficulty": case.get("difficulty", "unknown"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "answerable": False,
                    "answerability_correct": False,
                    "keyword_pass": False,
                    "concept_coverage_pass": False,
                    "citation_hit": False,
                    "chinese_answer": False,
                    "latency_ms": 0.0,
                    "total_tokens": 0,
                    "image_tokens": 0,
                    "retriever": None,
                    "expected_mode": case.get("expected_mode"),
                    "actual_mode": None,
                    "routing_correct": False if case.get("expected_mode") else None,
                    "fell_back": False,
                    "model": None,
                }
            )
            print(f"{case['id']}: ERROR {type(exc).__name__}", flush=True)
            continue
        answer = result["answer"]
        cited = {
            evidence_id
            for citation in result["citations"]
            for evidence_id in citation.get("evidence_ids", [])
        }
        expected_ids = {
            f"{case['source_id']}#{item_id}"
            for item_id in case["evidence_item_ids"]
        }
        cited_ids = {
            f"{value.split('@', 1)[0]}#{value.rsplit('#', 1)[-1]}"
            for value in cited
            if "@" in value and "#" in value
        }
        citation_hit = (
            bool(expected_ids & cited_ids)
            if expected_ids
            else not cited
        )
        keyword_pass = all(keyword.casefold() in answer.casefold() for keyword in case["required_keywords"])
        concept_pass = concept_coverage_pass(answer, case)
        answerable = result["answer_mode"] != "abstention"
        retrieval = result.get("retrieval", {})
        actual_mode = retrieval.get("mode")
        expected_mode = case.get("expected_mode")
        fell_back = bool(retrieval.get("fallback_reason")) or (
            args.retrieval_mode != "auto"
            and actual_mode is not None
            and actual_mode != args.retrieval_mode
        )
        rows.append(
            {
                "id": case["id"],
                "source_id": case["source_id"],
                "modality": case["modality"],
                "difficulty": case.get("difficulty", "unknown"),
                "answer": answer,
                "answerable": answerable,
                "answerability_correct": answerable == case["expected_answerable"],
                "keyword_pass": keyword_pass if case["expected_answerable"] else True,
                "concept_coverage_pass": concept_pass if case["expected_answerable"] else True,
                "citation_hit": citation_hit,
                "chinese_answer": any("\u4e00" <= char <= "\u9fff" for char in answer),
                "latency_ms": result["latency_ms"],
                "total_tokens": int(result.get("usage", {}).get("total_tokens", 0)),
                "image_tokens": int(
                    result.get("usage", {}).get("prompt_tokens_details", {}).get("image_tokens", 0)
                ),
                "retriever": result["retriever"],
                "retrieval": retrieval,
                "expected_mode": expected_mode,
                "actual_mode": actual_mode,
                "routing_correct": (
                    actual_mode == expected_mode if expected_mode else None
                ),
                "fell_back": fell_back,
                "model": result["model"],
            }
        )
        print(f"{case['id']}: answerable={answerable} citation={citation_hit}", flush=True)
    latencies = [row["latency_ms"] for row in rows]
    answerable_rows = [
        row for row, case in zip(rows, cases) if case["expected_answerable"]
    ]
    routed_rows = [row for row in rows if row.get("expected_mode")]
    metrics = {
        "cases": len(rows),
        "answerability_accuracy": round(sum(row["answerability_correct"] for row in rows) / len(rows), 4),
        "keyword_answer_accuracy": round(sum(row["keyword_pass"] for row in answerable_rows) / len(answerable_rows), 4),
        "concept_coverage_accuracy": round(
            sum(row["concept_coverage_pass"] for row in answerable_rows) / len(answerable_rows),
            4,
        ),
        "citation_accuracy": round(sum(row["citation_hit"] for row in rows) / len(rows), 4),
        "chinese_answer_rate": round(sum(row["chinese_answer"] for row in rows) / len(rows), 4),
        "latency_ms_mean": round(statistics.mean(latencies), 2),
        "latency_ms_p95": round(percentile(latencies, 0.95), 2),
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "image_tokens": sum(row["image_tokens"] for row in rows),
        "fallback_count": sum(row.get("fell_back", False) for row in rows),
        "routing_accuracy": round(
            sum(bool(row.get("routing_correct")) for row in routed_rows)
            / len(routed_rows),
            4,
        ) if routed_rows else None,
    }
    metrics["fallback_rate"] = round(metrics["fallback_count"] / len(rows), 4)
    payload = {
        "suite": str(args.suite),
        "retrieval_mode": args.retrieval_mode,
        "evaluation_scope": args.scope,
        "provider": args.provider,
        "metrics": metrics,
        "metrics_by_modality": grouped_metrics(rows, cases, "modality"),
        "metrics_by_difficulty": grouped_metrics(rows, cases, "difficulty"),
        "metrics_by_source": grouped_metrics(rows, cases, "source_id"),
        "valid_for_requested_mode": not bool(metrics["fallback_count"]),
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if metrics["fallback_count"] and not args.allow_fallback:
        print("在线评测包含检索回退，不能作为请求模式的有效结果", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
