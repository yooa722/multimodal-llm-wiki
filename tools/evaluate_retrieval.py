from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.pipeline import WikiPipeline
from mmwiki.retrieval import RETRIEVAL_MODES


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def first_relevant_rank(hits: list[dict[str, Any]], case: dict[str, Any]) -> int | None:
    expected = set(case["evidence_item_ids"])
    return next(
        (
            index
            for index, hit in enumerate(hits, 1)
            if hit["source_id"] == case["source_id"]
            and expected & set(hit["item_ids"])
        ),
        None,
    )


def first_navigation_rank(trace: dict[str, Any], source_id: str) -> int | None:
    semantic_rank = next(
        (
            int(value.get("rank", index))
            for index, value in enumerate(
                trace.get("wiki_navigation_sources", []), 1
            )
            if value.get("source_id") == source_id
        ),
        None,
    )
    if semantic_rank is not None:
        return semantic_rank
    return next(
        (
            index
            for index, page in enumerate(trace.get("wiki_navigation", []), 1)
            if source_id in page.get("source_ids", [])
        ),
        None,
    )


def first_wiki_page_rank(trace: dict[str, Any], case: dict[str, Any]) -> int | None:
    expected = set(case.get("wiki_page_paths", []))
    if not expected:
        return None
    return next(
        (
            index
            for index, page in enumerate(trace.get("wiki_navigation", []), 1)
            if page.get("path") in expected
        ),
        None,
    )


def grouped_metrics(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in sorted({str(row.get(field) or "unknown") for row in rows}):
        selected = [row for row in rows if str(row.get(field) or "unknown") == value]
        ranks = [
            row["first_relevant_rank"]
            for row in selected
            if row["first_relevant_rank"]
        ]
        result[value] = {
            "cases": len(selected),
            "recall_at_k": round(sum(row["hit"] for row in selected) / len(selected), 4),
            "mrr": round(sum(1 / rank for rank in ranks) / len(selected), 4),
            "top1_accuracy": round(
                sum(row["first_relevant_rank"] == 1 for row in selected)
                / len(selected),
                4,
            ),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="评测多模态 Wiki 证据检索")
    parser.add_argument("--suite", type=Path, default=Path("evaluation/demo_qa.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("reports/retrieval-results.json"))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--wiki-source-k", type=int, default=3)
    parser.add_argument("--wiki-page-k", type=int, default=5)
    parser.add_argument(
        "--scope",
        choices=("corpus", "source"),
        default="corpus",
        help="corpus 为全库检索；source 仅用于已知来源内定位诊断",
    )
    parser.add_argument(
        "--retrieval-mode", choices=RETRIEVAL_MODES, default="lexical"
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="允许请求模式回退；默认回退会使评测返回失败，防止误标指标",
    )
    args = parser.parse_args()
    pipeline = WikiPipeline(PROJECT_ROOT)
    cases = load_jsonl(args.suite)
    rows: list[dict[str, Any]] = []
    for case in cases:
        if not case["evidence_item_ids"]:
            continue
        started = time.perf_counter()
        result = pipeline.search_with_trace(
            case["question"],
            args.top_k,
            {case["source_id"]} if args.scope == "source" else None,
            args.retrieval_mode,
        )
        hits = result["hits"]
        latency = (time.perf_counter() - started) * 1000
        retrieved = [
            f"{hit['source_id']}#{item}"
            for hit in hits
            for item in hit["item_ids"]
        ]
        rank = first_relevant_rank(hits, case)
        navigation_rank = first_navigation_rank(result["retrieval"], case["source_id"])
        wiki_page_rank = first_wiki_page_rank(result["retrieval"], case)
        fell_back = result["retrieval"].get("mode") != args.retrieval_mode
        rows.append(
            {
                "id": case["id"],
                "source_id": case["source_id"],
                "modality": case["modality"],
                "difficulty": case.get("difficulty", "unknown"),
                "hit": rank is not None,
                "first_relevant_rank": rank,
                "first_wiki_navigation_rank": navigation_rank,
                "wiki_page_gold_available": bool(case.get("wiki_page_paths")),
                "first_wiki_page_rank": wiki_page_rank,
                "fell_back": fell_back,
                "latency_ms": round(latency, 3),
                "retrieved_item_ids": retrieved,
                "retrieval": result["retrieval"],
            }
        )
    latencies = [row["latency_ms"] for row in rows]
    ranks = [row["first_relevant_rank"] for row in rows if row["first_relevant_rank"]]
    navigation_hits = [
        row
        for row in rows
        if row["first_wiki_navigation_rank"]
        and row["first_wiki_navigation_rank"] <= args.wiki_source_k
    ]
    wiki_page_rows = [row for row in rows if row["wiki_page_gold_available"]]
    wiki_page_ranks = [
        row["first_wiki_page_rank"]
        for row in wiki_page_rows
        if row["first_wiki_page_rank"]
        and row["first_wiki_page_rank"] <= args.wiki_page_k
    ]
    fallback_count = sum(row["fell_back"] for row in rows)
    result = {
        "suite": str(args.suite),
        "top_k": args.top_k,
        "evaluation_scope": args.scope,
        "retrieval_mode": args.retrieval_mode,
        "cases": len(rows),
        "metrics": {
            "recall_at_k": round(sum(row["hit"] for row in rows) / len(rows), 4),
            "mrr": round(sum(1 / rank for rank in ranks) / len(rows), 4),
            "top1_accuracy": round(
                sum(row["first_relevant_rank"] == 1 for row in rows) / len(rows), 4
            ),
            "ndcg_at_k": round(
                sum(1 / math.log2(rank + 1) for rank in ranks) / len(rows), 4
            ),
            "wiki_source_recall_at_k": round(len(navigation_hits) / len(rows), 4),
            "wiki_source_k": args.wiki_source_k,
            "wiki_page_gold_cases": len(wiki_page_rows),
            "wiki_page_recall_at_k": round(
                len(wiki_page_ranks) / len(wiki_page_rows), 4
            ) if wiki_page_rows else None,
            "wiki_page_mrr": round(
                sum(1 / rank for rank in wiki_page_ranks) / len(wiki_page_rows), 4
            ) if wiki_page_rows else None,
            "wiki_page_k": args.wiki_page_k,
            "fallback_count": fallback_count,
            "fallback_rate": round(fallback_count / len(rows), 4),
            "latency_ms_mean": round(statistics.mean(latencies), 3),
            "latency_ms_p95": round(
                sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)], 3
            ),
        },
        "metrics_by_modality": grouped_metrics(rows, "modality"),
        "metrics_by_difficulty": grouped_metrics(rows, "difficulty"),
        "metrics_by_source": grouped_metrics(rows, "source_id"),
        "valid_for_requested_mode": fallback_count == 0,
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    if fallback_count and not args.allow_fallback:
        print(
            f"评测无效：{fallback_count}/{len(rows)} 题回退，未执行请求的 {args.retrieval_mode} 模式",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
