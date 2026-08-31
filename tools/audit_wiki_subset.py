#!/usr/bin/env python3
"""Audit a selected Wiki subset for coverage, provenance and visual readiness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.contracts import load_package
from mmwiki.config import resolve_visual_processing_policy


RICH_TYPES = {"image", "figure", "chart", "table", "equation", "formula"}


def read_ids(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def is_rich(item: Any) -> bool:
    return bool(
        item.item_type in RICH_TYPES
        or item.table
        or item.equation
        or item.asset_ids
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="检查选定来源的完整 Wiki 构建质量")
    parser.add_argument("packages_root", type=Path)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source-id-file", type=Path, required=True)
    parser.add_argument("--visual-scope-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    state = read_json(args.runtime_root / "state.json")
    source_ids = read_ids(args.source_id_file)
    visual_scope = read_json(args.visual_scope_file) if args.visual_scope_file else {}
    package_paths = {
        str(read_json(manifest).get("package_id") or manifest.parent.name): manifest.parent
        for manifest in args.packages_root.glob("*/manifest.json")
    }
    rows: list[dict[str, Any]] = []
    for source_id in source_ids:
        package = load_package(package_paths[source_id])
        source = (state.get("sources") or {}).get(source_id) or {}
        stages = source.get("stages") or {}
        item_map = {item.item_id: item for item in package.items}
        rich_items = [item for item in package.items if is_rich(item)]
        expected_assets = {
            asset_id for item in rich_items for asset_id in item.asset_ids
        }
        represented_items = {
            str(item.get("item_id") or "") for item in source.get("items", [])
        }
        represented_rich = sum(item.item_id in represented_items for item in rich_items)
        copied_assets = set(map(str, (source.get("assets") or {}).keys()))
        stable_paths = [
            path
            for path in source.get("generated_paths", [])
            if path in (state.get("pages") or {})
        ]
        valid_evidence = {
            f"{source_id}@{package.checksum[:12]}#{item.item_id}"
            for item in package.items
        }
        stable_refs = [
            str(ref)
            for path in stable_paths
            for ref in state["pages"][path].get("evidence_ids", [])
            if str(ref).startswith(source_id + "@")
        ]
        invalid_refs = sorted(set(stable_refs) - valid_evidence)
        selected_visual_items = set(map(str, visual_scope.get(source_id, [])))
        selected_assets: set[str] = set()
        for item_id in selected_visual_items:
            item = item_map.get(item_id)
            if item is None:
                continue
            policy = resolve_visual_processing_policy(
                item_type=item.item_type,
                has_structured_table=bool(item.table),
                has_latex=bool(item.equation),
                caption=item.caption,
                breadcrumb=item.breadcrumb,
                metadata=item.metadata,
            )
            if policy.run_caption or policy.run_ocr:
                selected_assets.update(item.asset_ids)
        ready_visual_assets = {
            str(record.get("asset_id") or "")
            for record in source.get("visual_evidence", [])
            if record.get("status") == "ready" and record.get("searchable")
        }
        source_page = args.runtime_root / "vault" / str(source.get("wiki_path") or "")
        evidence_page = args.runtime_root / "vault" / str(
            source.get("evidence_map_path") or ""
        )
        checks = {
            "text_api_complete": (
                stages.get("text", {}).get("status") == "completed"
                and stages.get("text", {}).get("provider") == "api"
            ),
            "stable_pages_present": bool(stable_paths),
            "multimodal_complete": stages.get("multimodal", {}).get("status")
            in {"completed", "not_applicable"},
            "all_items_represented": len(represented_items) == len(package.items),
            "all_rich_items_represented": represented_rich == len(rich_items),
            "all_assets_copied": expected_assets <= copied_assets,
            "source_page_present": source_page.is_file(),
            "evidence_page_present": evidence_page.is_file(),
            "stable_refs_valid": not invalid_refs,
            "selected_visual_assets_ready": selected_assets <= ready_visual_assets,
        }
        rows.append(
            {
                "source_id": source_id,
                "checks": checks,
                "passed": all(checks.values()),
                "counts": {
                    "items": len(package.items),
                    "rich_items": len(rich_items),
                    "represented_rich_items": represented_rich,
                    "assets": len(expected_assets),
                    "copied_assets": len(copied_assets & expected_assets),
                    "stable_pages": len(stable_paths),
                    "stable_evidence_refs": len(set(stable_refs)),
                    "selected_visual_assets": len(selected_assets),
                    "ready_selected_visual_assets": len(selected_assets & ready_visual_assets),
                },
                "invalid_evidence_refs": invalid_refs,
            }
        )

    result = {
        "sources": len(rows),
        "passed_sources": sum(row["passed"] for row in rows),
        "failed_sources": sum(not row["passed"] for row in rows),
        "rows": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failed_sources"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
