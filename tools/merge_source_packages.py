#!/usr/bin/env python3
"""Merge page-split mmwiki-0.1 packages back into one logical source."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mmwiki.contracts import load_package, validate_package  # noqa: E402
from tools.mineru_reference_to_package import safe_id


ITEM_ID = re.compile(r"^item-p(\d+)-b(\d+)$")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _jsonl(values: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values)


def _shift_item_id(item_id: str, offset: int) -> str:
    match = ITEM_ID.fullmatch(item_id)
    if not match:
        raise ValueError(f"无法平移非标准 Item ID：{item_id}")
    return f"item-p{int(match.group(1)) + offset:04d}-b{int(match.group(2)):04d}"


def _shift_page(value: object, offset: int) -> object:
    return value + offset if isinstance(value, int) else value


def merge_packages(
    package_paths: list[Path],
    page_offsets: list[int],
    output_root: Path,
    *,
    source_name: str,
    source_filename: str,
) -> Path:
    if not package_paths or len(package_paths) != len(page_offsets):
        raise ValueError("package_paths 与 page_offsets 必须非空且数量一致")
    if page_offsets != sorted(page_offsets) or page_offsets[0] != 0:
        raise ValueError("page_offsets 必须从 0 开始并按升序排列")
    source_name = source_name.strip()
    if not source_name:
        raise ValueError("source_name 不能为空")

    roots = [path.expanduser().resolve() for path in package_paths]
    for root in roots:
        validate_package(root)
    target = output_root.expanduser().resolve() / safe_id(source_name)
    if target.exists():
        raise ValueError(f"合并目标已存在：{target}")
    (target / "assets").mkdir(parents=True)
    (target / "raw").mkdir(parents=True)

    merged_items: list[dict[str, Any]] = []
    merged_chunks: list[dict[str, Any]] = []
    merged_assets: dict[str, dict[str, Any]] = {}
    sequence = 0
    chunk_sequence = 0

    for part_number, (root, offset) in enumerate(zip(roots, page_offsets, strict=True), 1):
        package = load_package(root)
        artifacts = package.manifest["artifacts"]
        item_values = _read_jsonl(root / artifacts["items"])
        chunk_values = _read_jsonl(root / artifacts["chunks"])
        assets_value = json.loads((root / artifacts["assets_index"]).read_text(encoding="utf-8"))
        if isinstance(assets_value, dict):
            assets_value = assets_value.get("assets", [])

        id_map = {
            str(item["item_id"]): _shift_item_id(str(item["item_id"]), offset)
            for item in item_values
        }
        for item in item_values:
            sequence += 1
            old_id = str(item["item_id"])
            item["item_id"] = id_map[old_id]
            item["sequence"] = sequence
            item["page_start"] = _shift_page(item.get("page_start"), offset)
            item["page_end"] = _shift_page(item.get("page_end"), offset)
            provenance = item.setdefault("provenance", {})
            provenance["source_part"] = part_number
            provenance["part_page"] = provenance.get("page")
            provenance["page"] = _shift_page(provenance.get("page"), offset)
            metadata = item.setdefault("metadata", {})
            relations = metadata.setdefault("relations", {})
            for key in ("previous_item_id", "next_item_id"):
                if relations.get(key) in id_map:
                    relations[key] = id_map[relations[key]]
            relations["context_item_ids"] = [
                id_map[value]
                for value in relations.get("context_item_ids", [])
                if value in id_map
            ]
            merged_items.append(item)

        for chunk in chunk_values:
            chunk_sequence += 1
            chunk["chunk_id"] = f"chunk-{safe_id(source_name)}-{chunk_sequence:05d}"
            chunk["item_ids"] = [id_map[value] for value in chunk.get("item_ids", [])]
            chunk["page_refs"] = [
                int(value) + offset for value in chunk.get("page_refs", [])
            ]
            provenance = chunk.setdefault("provenance", {})
            provenance["source_part"] = part_number
            provenance["item_ids"] = [
                id_map[value] for value in provenance.get("item_ids", []) if value in id_map
            ]
            provenance["context_item_ids"] = [
                id_map[value]
                for value in provenance.get("context_item_ids", [])
                if value in id_map
            ]
            provenance["pages"] = [
                int(value) + offset for value in provenance.get("pages", [])
            ]
            merged_chunks.append(chunk)

        for asset in assets_value:
            asset_id = str(asset["asset_id"])
            source = root / str(asset["path"])
            existing = merged_assets.get(asset_id)
            if existing:
                if existing.get("sha256") != asset.get("sha256"):
                    raise ValueError(f"重复 Asset ID 的 SHA-256 不一致：{asset_id}")
                continue
            suffix = source.suffix.lower() or ".bin"
            relative = f"assets/{asset_id}{suffix}"
            shutil.copy2(source, target / relative)
            merged_assets[asset_id] = {
                **asset,
                "path": relative,
                "source_path": f"part-{part_number:03d}/{asset.get('source_path', '')}",
            }

        raw_source = root / str(artifacts.get("raw") or "raw")
        if raw_source.is_dir():
            shutil.copytree(raw_source, target / "raw" / f"part-{part_number:03d}")

    for index, item in enumerate(merged_items):
        relations = item.setdefault("metadata", {}).setdefault("relations", {})
        if index:
            relations["previous_item_id"] = merged_items[index - 1]["item_id"]
        else:
            relations.pop("previous_item_id", None)
        if index + 1 < len(merged_items):
            relations["next_item_id"] = merged_items[index + 1]["item_id"]
        else:
            relations.pop("next_item_id", None)

    manifest = {
        "schema_version": "mmwiki-0.1",
        "package_id": safe_id(source_name),
        "document": {
            "title": source_name,
            "source": {
                "filename": source_filename,
                "structured_output": "raw/",
                "media_type": "application/pdf",
            },
        },
        "parser": {
            "name": "mineru",
            "version": "mineru-cloud-vlm-page-merge",
            "structured_output": "content_list_v2_parts",
        },
        "artifacts": {
            "items": "items.jsonl",
            "chunks": "chunks.jsonl",
            "raw": "raw/",
            "assets": "assets/",
            "assets_index": "assets.json",
        },
        "counts": {
            "records": len(merged_items),
            "chunks": len(merged_chunks),
            "assets": len(merged_assets),
        },
        "handoff": {
            "text_index": "chunks.text",
            "image_index": "chunks.asset_ids",
            "table_index": "items.content.table",
            "answer_citation": "chunks.provenance",
        },
        "quality": {
            "semantic_enrichment": "adjacent-text-for-visual-retrieval",
            "retrieval_proxy": "deterministic-mineru-adapter",
            "source_parts": len(roots),
            "page_offsets": page_offsets,
        },
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "items.jsonl").write_text(_jsonl(merged_items), encoding="utf-8")
    (target / "chunks.jsonl").write_text(_jsonl(merged_chunks), encoding="utf-8")
    (target / "assets.json").write_text(
        json.dumps(list(merged_assets.values()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    validate_package(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="合并分页解析的 mmwiki-0.1 Source Package")
    parser.add_argument("packages", nargs="+", type=Path)
    parser.add_argument("--page-offset", action="append", type=int, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--source-filename", required=True)
    args = parser.parse_args()
    output = merge_packages(
        args.packages,
        args.page_offset,
        args.output_root,
        source_name=args.source_name,
        source_filename=args.source_filename,
    )
    print(json.dumps({"status": "merged", "package": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
