from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

if __package__:
    from tools.mineru_reference_to_package import (
        inline_text,
        is_searchable_item,
        locate_asset,
        rows_from_html,
        safe_id,
        strings,
    )
else:
    from mineru_reference_to_package import (  # type: ignore[no-redef]
        inline_text,
        is_searchable_item,
        locate_asset,
        rows_from_html,
        safe_id,
        strings,
    )


VISUAL_TYPES = {"image", "chart", "table"}
CONTENT_LIST_V2_SUFFIX = "_content_list_v2.json"
CONTENT_LIST_SUFFIX = "_content_list.json"


@dataclass(frozen=True)
class MinerUBlock:
    page_number: int
    block_index: int
    raw_ref: str
    value: dict[str, Any]


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"MinerU 输出不是合法 JSON：{path}：{exc}") from exc


def discover_content_lists(source_root: Path) -> list[Path]:
    """Find MinerU structured outputs and prefer V2 when both formats exist."""

    source_root = source_root.expanduser().resolve()
    if source_root.is_file():
        if source_root.name.endswith((CONTENT_LIST_V2_SUFFIX, CONTENT_LIST_SUFFIX)):
            return [source_root]
        raise ValueError(f"不是 MinerU Content List：{source_root}")
    if not source_root.is_dir():
        raise ValueError(f"MinerU 输出目录不存在：{source_root}")

    v2_paths = sorted(source_root.rglob(f"*{CONTENT_LIST_V2_SUFFIX}"))
    selected: dict[tuple[Path, str], Path] = {}
    for path in v2_paths:
        stem = path.name[: -len(CONTENT_LIST_V2_SUFFIX)]
        selected[(path.parent.resolve(), stem)] = path.resolve()
    for path in sorted(source_root.rglob(f"*{CONTENT_LIST_SUFFIX}")):
        if path.name.endswith(CONTENT_LIST_V2_SUFFIX):
            continue
        stem = path.name[: -len(CONTENT_LIST_SUFFIX)]
        selected.setdefault((path.parent.resolve(), stem), path.resolve())
    return sorted(selected.values())


def _page_entries(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("content_list", "blocks", "items"):
            entries = value.get(key)
            if isinstance(entries, list):
                return entries
    return []


def iter_mineru_blocks(path: Path) -> list[MinerUBlock]:
    """Normalize current V2, grouped-page variants and legacy flat content lists."""

    data = _json(path)
    if isinstance(data, dict) and isinstance(data.get("pages"), list):
        data = data["pages"]
    if not isinstance(data, list):
        raise ValueError(f"MinerU Content List 顶层必须为数组：{path}")

    blocks: list[MinerUBlock] = []
    is_v2 = path.name.endswith(CONTENT_LIST_V2_SUFFIX)
    grouped_pages = bool(data) and all(
        isinstance(page, list)
        or (isinstance(page, dict) and bool(_page_entries(page)))
        for page in data
    )
    if grouped_pages:
        for page_offset, page in enumerate(data):
            page_entries = _page_entries(page)
            raw_page_idx = page.get("page_idx") if isinstance(page, dict) else None
            page_number = raw_page_idx + 1 if isinstance(raw_page_idx, int) else page_offset + 1
            for block_offset, raw in enumerate(page_entries):
                if not isinstance(raw, dict):
                    continue
                blocks.append(
                    MinerUBlock(
                        page_number=page_number,
                        block_index=block_offset + 1,
                        raw_ref=f"content_list_v2[{page_offset}][{block_offset}]",
                        value=raw,
                    )
                )
        return blocks

    page_counts: dict[int, int] = {}
    for raw_offset, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        raw_page_idx = raw.get("page_idx")
        page_number = raw_page_idx + 1 if isinstance(raw_page_idx, int) else 1
        page_counts[page_number] = page_counts.get(page_number, 0) + 1
        blocks.append(
            MinerUBlock(
                page_number=page_number,
                block_index=page_counts[page_number],
                raw_ref=f"content_list[{raw_offset}]",
                value=raw,
            )
        )
    return blocks


def _source_name(path: Path) -> str:
    for suffix in (CONTENT_LIST_V2_SUFFIX, CONTENT_LIST_SUFFIX):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.stem


def _kind(raw: dict[str, Any]) -> str:
    value = str(raw.get("type") or "text").strip().lower()
    try:
        text_level = int(raw.get("text_level") or 0)
    except (TypeError, ValueError):
        text_level = 0
    aliases = {
        "text": "title" if text_level > 0 else "paragraph",
        "equation": "equation",
        "equation_interline": "equation",
        "interline_equation": "equation",
        "header": "page_header",
        "footer": "page_footer",
        "aside_text": "page_aside_text",
    }
    return aliases.get(value, value)


def _content(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("content")
    return value if isinstance(value, dict) else {}


def _body_text(raw: dict[str, Any], kind: str) -> str:
    content = _content(raw)
    if kind == "title":
        return inline_text(content.get("title_content")) or inline_text(raw.get("text"))
    if kind == "paragraph":
        return inline_text(content.get("paragraph_content")) or inline_text(raw.get("text"))
    if kind == "equation":
        return (
            inline_text(content.get("math_content"))
            or inline_text(raw.get("text"))
            or inline_text(raw.get("latex"))
        )
    if kind == "code":
        return inline_text(content.get("code_content")) or inline_text(raw.get("code_body"))
    if kind in {"list", "index"}:
        return "\n".join(strings(content.get("list_items") or raw.get("list_items")))
    return inline_text(content.get("content")) or inline_text(raw.get("text"))


def _caption(raw: dict[str, Any], kind: str) -> str:
    content = _content(raw)
    keys = {
        "image": ("image_caption",),
        "chart": ("chart_caption",),
        "table": ("table_caption",),
        "code": ("code_caption",),
    }.get(kind, ())
    result: list[str] = []
    for key in keys:
        result.extend(strings(content.get(key)))
        result.extend(strings(raw.get(key)))
    return " ".join(dict.fromkeys(result))


def _table_value(raw: dict[str, Any]) -> tuple[str, list[list[str]]]:
    content = _content(raw)
    value = content.get("html") or content.get("table_body") or raw.get("table_body") or ""
    table_html = str(value) if value else ""
    return table_html, rows_from_html(table_html) if "<table" in table_html.lower() else []


def _asset_source(source_root: Path, raw: dict[str, Any]) -> Path | None:
    content = _content(raw)
    merged = dict(raw)
    merged.update(content)
    return locate_asset(source_root, merged)


def _copy_asset(
    source: Path,
    package_root: Path,
    assets: dict[str, dict[str, Any]],
    source_root: Path,
) -> str:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset_id = f"asset-{digest[:20]}"
    suffix = source.suffix.lower() or ".bin"
    relative = f"assets/{digest[:20]}{suffix}"
    target = package_root / relative
    if not target.exists():
        shutil.copy2(source, target)
    assets[asset_id] = {
        "asset_id": asset_id,
        "path": relative,
        "media_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "sha256": digest,
        "source_path": source.relative_to(source_root).as_posix(),
    }
    return asset_id


def _heading_level(raw: dict[str, Any]) -> int:
    content = _content(raw)
    value = content.get("level", raw.get("text_level", 1))
    try:
        return max(1, min(int(value), 6))
    except (TypeError, ValueError):
        return 1


def _context_for_visual(
    items: list[dict[str, Any]],
    index: int,
    *,
    context_chars: int,
) -> tuple[str, list[str]]:
    target = items[index]
    page = target["page_start"]
    breadcrumb = target["breadcrumb"]
    candidates: list[tuple[int, dict[str, Any]]] = []
    for distance in range(1, 4):
        for neighbor_index in (index - distance, index + distance):
            if not 0 <= neighbor_index < len(items):
                continue
            neighbor = items[neighbor_index]
            if neighbor["page_start"] != page:
                continue
            if breadcrumb and neighbor["breadcrumb"] and neighbor["breadcrumb"] != breadcrumb:
                continue
            text = str(neighbor["content"].get("raw_text") or "").strip()
            if text and neighbor["type"] not in VISUAL_TYPES | {"equation", "title"}:
                candidates.append((neighbor_index, neighbor))
    candidates.sort(key=lambda value: value[0])
    context_ids: list[str] = []
    context_parts: list[str] = []
    remaining = max(0, context_chars)
    for _, neighbor in candidates:
        if remaining <= 0:
            break
        text = str(neighbor["content"].get("raw_text") or "").strip()
        clipped = text[:remaining]
        if clipped:
            context_parts.append(clipped)
            context_ids.append(neighbor["item_id"])
            remaining -= len(clipped)
    return " ".join(context_parts), context_ids


def _iter_jsonl(values: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values)


def convert_content_list(
    content_list: Path,
    output_root: Path,
    *,
    context_chars: int = 1200,
    parser_version: str = "mineru",
    source_name: str | None = None,
    source_filename: str | None = None,
) -> Path:
    content_list = content_list.expanduser().resolve()
    source_root = content_list.parent
    output_root = output_root.expanduser().resolve()
    source_name = str(source_name or _source_name(content_list)).strip()
    if not source_name:
        raise ValueError("来源名称不能为空")
    source_filename = str(source_filename or f"{source_name}.pdf").strip()
    package_id = safe_id(source_name)
    target = output_root / package_id
    if target.exists():
        raise ValueError(f"目标 package 已存在，请换一个空输出目录：{target}")
    (target / "assets").mkdir(parents=True)
    (target / "raw").mkdir(parents=True)
    shutil.copy2(content_list, target / "raw" / content_list.name)

    blocks = iter_mineru_blocks(content_list)
    if not blocks:
        raise ValueError(f"MinerU Content List 没有有效内容块：{content_list}")

    items: list[dict[str, Any]] = []
    assets: dict[str, dict[str, Any]] = {}
    headings: list[str] = []
    for sequence, block in enumerate(blocks, 1):
        raw = block.value
        kind = _kind(raw)
        text = _body_text(raw, kind)
        if kind == "title":
            level = _heading_level(raw)
            headings[:] = headings[: level - 1]
            if text:
                headings.append(text)
        caption = _caption(raw, kind)
        table_html, table_rows = _table_value(raw) if kind == "table" else ("", [])
        asset_ids: list[str] = []
        asset_issue = ""
        try:
            asset_source = _asset_source(source_root, raw)
        except ValueError as exc:
            # MinerU occasionally emits an empty visual placeholder such as
            # ``images/`` for a table with no exported crop.  Preserve the
            # block and provenance, but never invent or silently cite an asset.
            if str(exc).startswith("资源不存在："):
                asset_source = None
                asset_issue = str(exc)
            else:
                raise
        if asset_source:
            asset_ids.append(_copy_asset(asset_source, target, assets, source_root))

        breadcrumb = " > ".join(headings)
        raw_text = text or caption
        search_parts = [breadcrumb, raw_text]
        if table_rows:
            search_parts.extend(cell for row in table_rows[:12] for cell in row[:12])
        elif table_html:
            search_parts.append(table_html)
        search_text = " ".join(part for part in search_parts if part).strip()
        item_id = f"item-p{block.page_number:04d}-b{block.block_index:04d}"
        bbox_values = raw.get("bbox", [])
        if not isinstance(bbox_values, list):
            bbox_values = []
        numeric_bbox = [value for value in bbox_values if isinstance(value, (int, float))]
        coordinate_system = (
            "normalized_1"
            if numeric_bbox and max(abs(value) for value in numeric_bbox) <= 1
            else "normalized_1000"
        )
        sub_type = str(raw.get("sub_type") or "").strip()
        item: dict[str, Any] = {
            "item_id": item_id,
            "sequence": sequence,
            "type": kind,
            "raw_type": str(raw.get("type") or kind),
            "page_start": block.page_number,
            "page_end": block.page_number,
            "bbox": {
                "values": bbox_values,
                "coordinate_system": coordinate_system,
                "origin": "top_left",
            },
            "breadcrumb": breadcrumb,
            "content": {
                "raw_text": raw_text,
                "caption": caption,
                "search_text": search_text,
                "semantic": {},
            },
            "assets": [{"asset_id": value} for value in asset_ids],
            "provenance": {
                "parser": "mineru",
                "raw_ref": block.raw_ref,
                "page": block.page_number,
                "block_index": block.block_index,
            },
            "metadata": {
                "resource_type": kind,
                "visual_type": sub_type or (kind if kind in VISUAL_TYPES else ""),
            },
            "quality": {
                "needs_review": bool(asset_issue),
                "review_reasons": [asset_issue] if asset_issue else [],
            },
            "retrieval": {
                "searchable": is_searchable_item(kind, search_text, asset_ids),
                "exclude": not is_searchable_item(kind, search_text, asset_ids),
            },
        }
        if table_html:
            item["content"]["table"] = {"html": table_html, "rows": table_rows}
        if kind == "equation":
            item["content"]["equation"] = {"latex": text, "math_type": "latex"}
        items.append(item)

    for index, item in enumerate(items):
        relations: dict[str, Any] = {}
        if index > 0:
            relations["previous_item_id"] = items[index - 1]["item_id"]
        if index + 1 < len(items):
            relations["next_item_id"] = items[index + 1]["item_id"]
        if item["type"] in VISUAL_TYPES:
            context_text, context_ids = _context_for_visual(
                items, index, context_chars=context_chars
            )
            if context_text:
                item["content"]["semantic"]["adjacent_text"] = context_text
                item["content"]["search_text"] = " ".join(
                    part
                    for part in (item["content"]["search_text"], context_text)
                    if part
                )
            relations["context_item_ids"] = context_ids
        item["metadata"]["relations"] = relations

    chunks: list[dict[str, Any]] = []
    for item in items:
        if not item["retrieval"]["searchable"]:
            continue
        relations = item["metadata"].get("relations", {})
        chunks.append(
            {
                "chunk_id": f"chunk-{package_id}-{item['sequence']:05d}",
                # Only the target item is cited. Adjacent items are retrieval context,
                # recorded separately so they cannot silently become answer evidence.
                "item_ids": [item["item_id"]],
                "text": item["content"]["search_text"],
                "breadcrumb": item["breadcrumb"],
                "modalities": [item["type"]],
                "asset_ids": [value["asset_id"] for value in item["assets"]],
                "page_refs": [item["page_start"]],
                "provenance": {
                    "item_ids": [item["item_id"]],
                    "pages": [item["page_start"]],
                    "context_item_ids": relations.get("context_item_ids", []),
                },
                "quality": {"needs_review": False},
            }
        )

    structured_name = f"raw/{content_list.name}"
    manifest = {
        "schema_version": "mmwiki-0.1",
        "package_id": package_id,
        "document": {
            "title": source_name,
            "source": {
                "filename": source_filename,
                "structured_output": structured_name,
                "media_type": "application/pdf",
            },
        },
        "parser": {
            "name": "mineru",
            "version": parser_version,
            "structured_output": (
                "content_list_v2"
                if content_list.name.endswith(CONTENT_LIST_V2_SUFFIX)
                else "content_list"
            ),
        },
        "artifacts": {
            "items": "items.jsonl",
            "chunks": "chunks.jsonl",
            "raw": "raw/",
            "assets": "assets/",
            "assets_index": "assets.json",
        },
        "counts": {"records": len(items), "chunks": len(chunks), "assets": len(assets)},
        "handoff": {
            "text_index": "chunks.text",
            "image_index": "chunks.asset_ids",
            "table_index": "items.content.table",
            "answer_citation": "chunks.provenance",
        },
        "quality": {
            "semantic_enrichment": "adjacent-text-for-visual-retrieval",
            "retrieval_proxy": "deterministic-mineru-adapter",
        },
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "items.jsonl").write_text(_iter_jsonl(items), encoding="utf-8")
    (target / "chunks.jsonl").write_text(_iter_jsonl(chunks), encoding="utf-8")
    (target / "assets.json").write_text(
        json.dumps(list(assets.values()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把标准 MinerU Content List 批量转换为 mmwiki-0.1 Source Package"
    )
    parser.add_argument("source_root", type=Path, help="MinerU 输出文件或输出根目录")
    parser.add_argument("output_root", type=Path, help="Source Package 输出目录")
    parser.add_argument(
        "--context-chars",
        type=int,
        default=1200,
        help="写入图片/图表检索代理的同页相邻文字上限，默认 1200",
    )
    parser.add_argument(
        "--parser-version",
        default="mineru",
        help="记录到 manifest 的 MinerU 版本或运行标识",
    )
    args = parser.parse_args()
    paths = discover_content_lists(args.source_root)
    if not paths:
        raise ValueError(f"没有找到 MinerU Content List：{args.source_root}")
    outputs = [
        str(
            convert_content_list(
                path,
                args.output_root,
                context_chars=max(0, args.context_chars),
                parser_version=args.parser_version,
            )
        )
        for path in paths
    ]
    print(json.dumps({"packages": outputs, "count": len(outputs)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
