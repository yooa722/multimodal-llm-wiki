from __future__ import annotations

import re
from typing import Any


PAGE_INDEX_SCHEMA = "mmwiki-page-index-0.1"
PARAGRAPH_ITEM_TYPES = {"paragraph", "page_aside_text", "page_footnote"}

_RAW_REF_PATTERN = re.compile(r"content_list_v2\[(\d+)\]\[(\d+)\]")
_ITEM_ID_PATTERN = re.compile(r"-p(\d+)-b(\d+)$")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _raw_position(item: dict[str, Any]) -> tuple[int | None, int | None]:
    raw_ref = str((item.get("provenance") or {}).get("raw_ref") or "")
    match = _RAW_REF_PATTERN.search(raw_ref)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _ITEM_ID_PATTERN.search(str(item.get("item_id") or ""))
    if match:
        return max(0, int(match.group(1)) - 1), max(0, int(match.group(2)) - 1)
    return None, None


def _item_type_label(item_type: str) -> str:
    return {
        "paragraph": "正文段落",
        "page_aside_text": "旁注段落",
        "page_footnote": "脚注段落",
        "title": "标题",
        "image": "图片区域",
        "figure": "图片区域",
        "chart": "图表区域",
        "table": "表格区域",
        "equation": "公式区域",
        "formula": "公式区域",
        "page_header": "页眉",
        "page_footer": "页脚",
        "page_number": "页码",
    }.get(item_type, "内容块")


def _location_label(
    page_number: int,
    item_type: str,
    block_index: int,
    paragraph_index: int | None,
) -> str:
    if paragraph_index is not None:
        return f"第 {page_number} 页 · 第 {paragraph_index} 段"
    type_label = _item_type_label(item_type)
    if type_label == "内容块":
        type_label = f"第 {block_index} 个内容块"
    return f"第 {page_number} 页 · {type_label}"


def build_evidence_page_index(state: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic page/block locator index from immutable items."""

    indexed_sources: dict[str, Any] = {}
    total_items = 0
    located_items = 0
    paragraph_items = 0

    for source_id in sorted(map(str, state.get("sources", {}))):
        source = state["sources"].get(source_id) or {}
        source_version = str(source.get("source_version") or "")
        items = [item for item in source.get("items", []) if isinstance(item, dict)]
        items.sort(
            key=lambda item: (
                _integer(item.get("page_start")) or 10**9,
                _integer(item.get("sequence")) or 0,
                str(item.get("item_id") or ""),
            )
        )
        total_items += len(items)
        pages: dict[str, Any] = {}
        unlocated_items: list[dict[str, str]] = []
        paragraph_counts: dict[int, int] = {}
        block_counts: dict[int, int] = {}

        for item in items:
            page_number = _integer(item.get("page_start"))
            if page_number is None or page_number < 1:
                unlocated_items.append(
                    {
                        "evidence_id": (
                            f"{source_id}@{source_version}#{item.get('item_id', '')}"
                        ),
                        "item_id": str(item.get("item_id") or ""),
                        "reason": "Source Package 未提供有效 page_start",
                    }
                )
                continue
            raw_page_index, raw_block_index = _raw_position(item)
            page_index = (
                raw_page_index if raw_page_index is not None else page_number - 1
            )
            block_counts[page_number] = block_counts.get(page_number, 0) + 1
            block_index = (
                raw_block_index + 1
                if raw_block_index is not None
                else block_counts[page_number]
            )
            item_type = str(item.get("item_type") or item.get("type") or "text")
            paragraph_index: int | None = None
            if item_type in PARAGRAPH_ITEM_TYPES:
                paragraph_counts[page_number] = paragraph_counts.get(page_number, 0) + 1
                paragraph_index = paragraph_counts[page_number]
                paragraph_items += 1

            evidence_id = f"{source_id}@{source_version}#{item.get('item_id', '')}"
            text = str(
                item.get("raw_text")
                or item.get("caption")
                or item.get("search_text")
                or ""
            ).strip()
            provenance = item.get("provenance") or {}
            locator = {
                "evidence_id": evidence_id,
                "source_id": source_id,
                "source_version": source_version,
                "item_id": str(item.get("item_id") or ""),
                "item_type": item_type,
                "page_index": page_index,
                "page_number": page_number,
                "page_end": _integer(item.get("page_end")) or page_number,
                "block_index": block_index,
                "paragraph_index": paragraph_index,
                "location_label": _location_label(
                    page_number, item_type, block_index, paragraph_index
                ),
                "breadcrumb": str(item.get("breadcrumb") or ""),
                "bbox": item.get("bbox") if isinstance(item.get("bbox"), dict) else {},
                "raw_ref": str(provenance.get("raw_ref") or ""),
                "quote": text[:1200],
                "asset_ids": list(map(str, item.get("asset_ids", []))),
            }
            page_key = str(page_number)
            page = pages.setdefault(
                page_key,
                {
                    "page_key": f"{source_id}@{source_version}:p{page_number:04d}",
                    "page_index": page_index,
                    "page_number": page_number,
                    "items": [],
                },
            )
            page["items"].append(locator)
            located_items += 1

        indexed_sources[source_id] = {
            "source_id": source_id,
            "source_version": source_version,
            "title": str(source.get("title") or source_id),
            "pages": pages,
            "unlocated_items": unlocated_items,
        }

    return {
        "schema_version": PAGE_INDEX_SCHEMA,
        "source_state_schema": str(state.get("schema_version") or ""),
        "sources": indexed_sources,
        "stats": {
            "sources": len(indexed_sources),
            "items": total_items,
            "located_items": located_items,
            "unlocated_items": total_items - located_items,
            "paragraph_items": paragraph_items,
            "coverage": round(located_items / total_items, 6) if total_items else 1.0,
        },
    }


def evidence_locator_lookup(page_index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in page_index.get("sources", {}).values():
        for page in (source or {}).get("pages", {}).values():
            for item in (page or {}).get("items", []):
                evidence_id = str((item or {}).get("evidence_id") or "")
                if evidence_id:
                    result[evidence_id] = item
    return result
