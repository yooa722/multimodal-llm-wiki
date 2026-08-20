"""Shared representation for image-derived searchable Evidence."""

from __future__ import annotations

from typing import Any, Iterable


def visual_evidence_id(
    source_id: str, source_version: str, asset_id: str, kind: str
) -> str:
    return f"{source_id}@{source_version}#{asset_id}#{kind}"


def iter_visual_evidence(source: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for record in source.get("visual_evidence", []):
        if not isinstance(record, dict) or not record.get("searchable", True):
            continue
        if str(record.get("status") or "ready") != "ready":
            continue
        if not str(record.get("text") or "").strip():
            continue
        yield record


def synthetic_visual_chunks(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose derived OCR/Caption as child chunks without changing source chunks."""

    result: list[dict[str, Any]] = []
    for record in iter_visual_evidence(source):
        kind = str(record.get("kind") or "")
        parent_chunks = [str(value) for value in record.get("parent_chunk_ids", [])]
        parent_items = [str(value) for value in record.get("parent_item_ids", [])]
        result.append(
            {
                "chunk_id": str(record.get("id") or ""),
                "parent_chunk_id": parent_chunks[0] if parent_chunks else "",
                "breadcrumb": str(record.get("breadcrumb") or "图片派生证据"),
                "text": str(record.get("text") or ""),
                "item_ids": parent_items,
                "asset_ids": [str(record.get("asset_id") or "")]
                if record.get("asset_id")
                else [],
                "modalities": [kind] if kind else [],
                "page_refs": [int(value) for value in record.get("page_refs", [])],
                "provenance": record.get("provenance") or {},
                "quality": {"derived": True},
            }
        )
    return result


def iter_retrieval_chunks(
    source: dict[str, Any], *, include_derived: bool = True
) -> Iterable[dict[str, Any]]:
    yield from source.get("chunks", [])
    if include_derived:
        yield from synthetic_visual_chunks(source)


def visual_evidence_for_asset(
    source: dict[str, Any], asset_id: str, kind: str = ""
) -> list[dict[str, Any]]:
    return [
        record
        for record in iter_visual_evidence(source)
        if str(record.get("asset_id") or "") == str(asset_id)
        and (not kind or str(record.get("kind") or "") == kind)
    ]
