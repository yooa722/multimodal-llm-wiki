#!/usr/bin/env python3
"""Generate reviewable Wiki/Evidence candidates for an offline QA set.

This script is an annotation aid, not an evaluation runner.  It may use the
gold answer to improve candidate recall, but the generated file stays under
``evaluation/pending`` and must never be supplied to the query-time model.
Human review is still required before a case can enter the formal benchmark.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mmwiki.pipeline import WikiPipeline  # noqa: E402


SearchFunction = Callable[[str, int], dict[str, Any]]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(rows: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _compact_hit(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": str(hit.get("source_id") or ""),
        "chunk_id": str(hit.get("chunk_id") or ""),
        "item_ids": [str(value) for value in hit.get("item_ids", [])],
        "page_refs": [int(value) for value in hit.get("pages", [])],
        "modalities": [str(value) for value in hit.get("modalities", [])],
        "score": round(float(hit.get("score") or 0.0), 6),
        "title": str(hit.get("title") or ""),
        "snippet": str(hit.get("snippet") or "")[:1200],
        "wiki_page_paths": [str(value) for value in hit.get("wiki_paths", [])],
        "asset_paths": [str(value) for value in hit.get("asset_paths", [])],
    }


def map_case_candidates(
    case: dict[str, Any],
    search: SearchFunction,
    *,
    top_k: int = 12,
) -> dict[str, Any]:
    """Attach local retrieval candidates without asserting a gold source."""
    question = str(case.get("question") or "").strip()
    expected = str(case.get("expected_answer") or "").strip()
    if not question or not expected:
        raise ValueError("候选映射要求 question 和 expected_answer 均非空")

    # Gold text is used only to locate a review candidate offline.  Formal
    # evaluation continues to use the original question alone.
    annotation_query = f"{question}\n离线标注参考答案：{expected}"
    result = search(annotation_query, top_k)
    hits = [_compact_hit(hit) for hit in result.get("hits", [])]

    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "best_score": 0.0,
            "hit_count": 0,
            "page_refs": set(),
            "modalities": set(),
            "wiki_page_paths": set(),
        }
    )
    for hit in hits:
        source_id = hit["source_id"]
        if not source_id:
            continue
        source = grouped[source_id]
        source["best_score"] = max(source["best_score"], hit["score"])
        source["hit_count"] += 1
        source["page_refs"].update(hit["page_refs"])
        source["modalities"].update(hit["modalities"])
        source["wiki_page_paths"].update(hit["wiki_page_paths"])

    candidate_sources = [
        {
            "source_id": source_id,
            "best_score": round(values["best_score"], 6),
            "hit_count": values["hit_count"],
            "page_refs": sorted(values["page_refs"]),
            "modalities": sorted(values["modalities"]),
            "wiki_page_paths": sorted(values["wiki_page_paths"]),
        }
        for source_id, values in grouped.items()
    ]
    candidate_sources.sort(
        key=lambda value: (-value["best_score"], -value["hit_count"], value["source_id"])
    )

    mapped = dict(case)
    mapped.update(
        {
            "candidate_sources": candidate_sources,
            "candidate_evidence": hits,
            "candidate_retrieval": {
                "mode": result.get("retrieval", {}).get("mode", "lexical"),
                "channels": result.get("retrieval", {}).get("channels", []),
                "query_uses_expected_answer": True,
                "purpose": "offline_annotation_only",
            },
            "annotation_status": "candidate_generated_human_review_required",
        }
    )
    return mapped


def candidate_summary(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(cases)
    with_candidates = [row for row in rows if row.get("candidate_sources")]
    top_sources: dict[str, int] = defaultdict(int)
    top_modalities: dict[str, int] = defaultdict(int)
    for row in with_candidates:
        top = row["candidate_sources"][0]
        top_sources[str(top["source_id"])] += 1
        for modality in top.get("modalities", []):
            top_modalities[str(modality)] += 1
    return {
        "case_count": len(rows),
        "cases_with_candidate": len(with_candidates),
        "cases_without_candidate": len(rows) - len(with_candidates),
        "unique_top1_sources": len(top_sources),
        "top1_source_distribution": dict(
            sorted(top_sources.items(), key=lambda value: (-value[1], value[0]))
        ),
        "top1_modality_distribution": dict(sorted(top_modalities.items())),
        "status": "human_review_required",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为待标注评测集生成 Wiki/Evidence 候选")
    parser.add_argument("input", type=Path, help="待标注 JSONL")
    parser.add_argument("--runtime-root", type=Path, required=True, help="隔离 Runtime")
    parser.add_argument("--output", type=Path, required=True, help="候选 JSONL")
    parser.add_argument("--summary-output", type=Path, help="候选统计 JSON")
    parser.add_argument("--top-k", type=int, default=12, choices=range(1, 21))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pipeline = WikiPipeline(ROOT, runtime_root=args.runtime_root)

    def search(query: str, top_k: int) -> dict[str, Any]:
        return pipeline.search_with_trace(
            query,
            top_k=top_k,
            retrieval_mode="lexical",
        )

    source_cases = read_jsonl(args.input)
    mapped_cases: list[dict[str, Any]] = []
    for index, case in enumerate(source_cases, 1):
        mapped_cases.append(map_case_candidates(case, search, top_k=args.top_k))
        print(f"[{index}/{len(source_cases)}] {case.get('id')}", flush=True)
    write_jsonl(mapped_cases, args.output)

    summary = candidate_summary(mapped_cases)
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
