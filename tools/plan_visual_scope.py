#!/usr/bin/env python3
"""Plan cost-aware OCR/VLM work from Wiki pages and question-only retrieval.

The plan never reads reference answers.  All multimodal items still enter the
source/evidence Wiki; this file only limits expensive OCR/VLM enrichment.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.contracts import load_package
from mmwiki.pipeline import WikiPipeline


RICH_TYPES = {"image", "figure", "chart", "table", "equation", "formula"}


def read_source_ids(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_questions(path: Path) -> list[dict[str, str]]:
    questions: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        question = str(value.get("question") or "").strip()
        if question:
            questions.append({"id": str(value.get("id") or ""), "question": question})
    return questions


def is_rich(item: Any) -> bool:
    return bool(
        item.item_type in RICH_TYPES
        or item.table
        or item.equation
        or item.asset_ids
    )


def item_id_from_evidence(evidence_id: str) -> str:
    return evidence_id.rsplit("#", 1)[-1] if "#" in evidence_id else ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用稳定知识页与问题本身规划 OCR/VLM 范围，不读取参考答案"
    )
    parser.add_argument("packages_root", type=Path)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source-id-file", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--min-hit-score", type=float, default=18.0)
    parser.add_argument("--relative-hit-score", type=float, default=0.65)
    parser.add_argument("--max-image-items-per-source", type=int, default=20)
    parser.add_argument("--min-image-items-per-source", type=int, default=2)
    args = parser.parse_args()

    source_ids = read_source_ids(args.source_id_file)
    selected = set(source_ids)
    package_paths = {
        str(json.loads((manifest).read_text(encoding="utf-8"))["package_id"]): manifest.parent
        for manifest in args.packages_root.glob("*/manifest.json")
    }
    missing = sorted(selected - set(package_paths))
    if missing:
        raise SystemExit("未找到 Source Package：" + "、".join(missing))

    packages = {source_id: load_package(package_paths[source_id]) for source_id in source_ids}
    item_maps = {
        source_id: {item.item_id: item for item in package.items}
        for source_id, package in packages.items()
    }
    state_path = args.runtime_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    ordered: dict[str, list[str]] = {source_id: [] for source_id in source_ids}
    reasons: dict[str, dict[str, set[str]]] = {
        source_id: defaultdict(set) for source_id in source_ids
    }
    image_counts = defaultdict(int)

    def add(source_id: str, item_id: str, reason: str) -> None:
        item = item_maps[source_id].get(item_id)
        if item is None or not is_rich(item):
            return
        if item_id in ordered[source_id]:
            reasons[source_id][item_id].add(reason)
            return
        if item.asset_ids and image_counts[source_id] >= args.max_image_items_per_source:
            return
        ordered[source_id].append(item_id)
        reasons[source_id][item_id].add(reason)
        if item.asset_ids:
            image_counts[source_id] += 1

    # Stable pages are the Wiki knowledge skeleton; their cited rich Evidence
    # receives first priority for deeper visual semantics.
    for page in state.get("pages", {}).values():
        page_sources = set(map(str, page.get("source_ids", []))) & selected
        for source_id in page_sources:
            for evidence_id in page.get("evidence_ids", []):
                if str(evidence_id).startswith(source_id + "@"):
                    add(source_id, item_id_from_evidence(str(evidence_id)), "stable_page")

    pipeline = WikiPipeline(PROJECT_ROOT, runtime_root=args.runtime_root)
    questions = read_questions(args.questions)
    query_hits = 0
    accepted_questions = 0
    for question in questions:
        result = pipeline.search_with_trace(
            question["question"],
            top_k=args.top_k,
            source_ids=selected,
            retrieval_mode="lexical",
        )
        hits = result.get("hits", [])
        if not hits:
            continue
        best_score = float(hits[0].get("score") or 0.0)
        best_source = str(hits[0].get("source_id") or "")
        if best_source not in selected or best_score < args.min_hit_score:
            continue
        accepted_questions += 1
        accepted_hits = [
            hit
            for hit in hits
            if str(hit.get("source_id") or "") == best_source
            and float(hit.get("score") or 0.0)
            >= best_score * args.relative_hit_score
        ][:4]
        for hit in accepted_hits:
            source_id = str(hit.get("source_id") or "")
            if source_id not in selected:
                continue
            package = packages[source_id]
            by_sequence = {item.sequence: item for item in package.items}
            for hit_item_id in hit.get("item_ids", []):
                hit_item = item_maps[source_id].get(str(hit_item_id))
                if hit_item is None:
                    continue
                query_hits += 1
                add(source_id, hit_item.item_id, f"query:{question['id']}")
                # Text and its explanatory picture are often adjacent or share a
                # page.  Add the nearest rich neighbors without using the answer.
                candidates = [
                    item
                    for item in package.items
                    if is_rich(item)
                    and (
                        item.page_start == hit_item.page_start
                        or abs(item.sequence - hit_item.sequence) <= 2
                    )
                ]
                candidates.sort(
                    key=lambda item: (
                        item.page_start != hit_item.page_start,
                        abs(item.sequence - hit_item.sequence),
                        item.sequence,
                    )
                )
                for neighbor in candidates[:3]:
                    add(source_id, neighbor.item_id, f"query_neighbor:{question['id']}")

    # A general-purpose Wiki must not leave an image-bearing source with only
    # asset links and no derived visual semantics merely because the current
    # question set did not retrieve it.  Add a small, deterministic bootstrap
    # sample per source; this is still cost-bounded and never limits source-page
    # or Evidence coverage.
    type_priority = {"image": 0, "figure": 0, "chart": 1, "table": 2}
    for source_id, package in packages.items():
        if image_counts[source_id] >= args.min_image_items_per_source:
            continue
        candidates = [item for item in package.items if item.asset_ids]
        candidates.sort(
            key=lambda item: (
                type_priority.get(item.item_type, 3),
                item.page_start,
                item.sequence,
            )
        )
        for item in candidates:
            add(source_id, item.item_id, "source_bootstrap")
            if image_counts[source_id] >= args.min_image_items_per_source:
                break

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "method": "stable_page_refs + question_only_lexical_hits + nearby_rich_items + source_bootstrap",
        "uses_reference_answers": False,
        "sources": {
            source_id: {
                "selected_items": len(ordered[source_id]),
                "selected_image_items": image_counts[source_id],
                "items": [
                    {
                        "item_id": item_id,
                        "page": item_maps[source_id][item_id].page_start,
                        "type": item_maps[source_id][item_id].item_type,
                        "asset_count": len(item_maps[source_id][item_id].asset_ids),
                        "reasons": sorted(reasons[source_id][item_id]),
                    }
                    for item_id in ordered[source_id]
                ],
            }
            for source_id in source_ids
        },
        "questions": len(questions),
        "accepted_questions": accepted_questions,
        "retrieval_hit_items_examined": query_hits,
    }
    report_path = args.output.with_suffix(args.output.suffix + ".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"scope": str(args.output), "report": str(report_path), **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
