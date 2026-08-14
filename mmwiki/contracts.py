from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import Asset, Chunk, Item, Package


class ContractError(ValueError):
    pass


SUPPORTED_SCHEMA = "mmwiki-0.1"


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} 必须是 JSON 对象")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractError(f"{name} 必须是字符串数组")
    return list(dict.fromkeys(value))


def _safe_path(root: Path, relative: str, name: str, *, directory: bool = False) -> Path:
    raw = Path(relative)
    if not relative or raw.is_absolute() or ".." in raw.parts:
        raise ContractError(f"{name} 必须是 package 内的安全相对路径")
    target = (root / raw).resolve()
    if root.resolve() not in target.parents:
        raise ContractError(f"{name} 发生路径逃逸：{relative}")
    if directory and not target.is_dir():
        raise ContractError(f"{name} 目录不存在：{relative}")
    if not directory and not target.is_file():
        raise ContractError(f"{name} 文件不存在：{relative}")
    return target


def _read_json(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"{name} 不是合法 JSON：{exc}") from exc


def _read_jsonl(path: Path, name: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"{name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
        result.append(_object(value, f"{name} 第 {line_number} 行"))
    return result


def _asset(value: dict[str, Any], root: Path) -> Asset:
    asset_id = str(value.get("asset_id") or "").strip()
    path = str(value.get("path") or "").strip()
    media_type = str(value.get("media_type") or value.get("mime_type") or "").strip()
    if not asset_id or not path or not media_type:
        raise ContractError("assets.json 中的 asset_id、path、media_type 不能为空")
    target = _safe_path(root, path, f"asset {asset_id}")
    expected = str(value.get("sha256") or "").strip().lower()
    if expected:
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            raise ContractError(f"asset {asset_id} 的 SHA-256 不匹配")
    return Asset(
        asset_id=asset_id,
        path=path,
        media_type=media_type,
        sha256=expected,
        source_path=str(value.get("source_path") or value.get("original_path") or ""),
    )


def _item(value: dict[str, Any]) -> Item:
    item_id = str(value.get("item_id") or "").strip()
    if not item_id:
        raise ContractError("items.jsonl 的 item_id 不能为空")
    content = _object(value.get("content", {}), f"item {item_id}.content")
    assets_value = value.get("assets", [])
    if not isinstance(assets_value, list):
        raise ContractError(f"item {item_id}.assets 必须是数组")
    asset_ids: list[str] = []
    for asset in assets_value:
        if isinstance(asset, str):
            asset_id = asset
        elif isinstance(asset, dict):
            asset_id = str(asset.get("asset_id") or "")
        else:
            raise ContractError(f"item {item_id}.assets 含无效记录")
        if asset_id:
            asset_ids.append(asset_id)
    page_start = value.get("page_start")
    page_end = value.get("page_end")
    return Item(
        item_id=item_id,
        sequence=int(value.get("sequence", 0)),
        item_type=str(value.get("type") or "text"),
        page_start=int(page_start) if isinstance(page_start, int) else None,
        page_end=int(page_end) if isinstance(page_end, int) else None,
        bbox=_object(value.get("bbox", {}), f"item {item_id}.bbox"),
        breadcrumb=str(value.get("breadcrumb") or ""),
        raw_text=str(content.get("raw_text") or ""),
        caption=str(content.get("caption") or ""),
        search_text=str(content.get("search_text") or ""),
        table=content.get("table") if isinstance(content.get("table"), dict) else None,
        equation=(
            content.get("equation") if isinstance(content.get("equation"), dict) else None
        ),
        semantic=(
            content.get("semantic") if isinstance(content.get("semantic"), dict) else {}
        ),
        asset_ids=list(dict.fromkeys(asset_ids)),
        provenance=_object(value.get("provenance", {}), f"item {item_id}.provenance"),
        quality=_object(value.get("quality", {}), f"item {item_id}.quality"),
        searchable=bool(
            _object(value.get("retrieval", {}), f"item {item_id}.retrieval").get(
                "searchable", True
            )
        )
        and not bool(value.get("retrieval", {}).get("exclude", False)),
        metadata=_object(value.get("metadata", {}), f"item {item_id}.metadata"),
    )


def _chunk(value: dict[str, Any]) -> Chunk:
    chunk_id = str(value.get("chunk_id") or "").strip()
    if not chunk_id:
        raise ContractError("chunks.jsonl 的 chunk_id 不能为空")
    pages = value.get("page_refs", [])
    if not isinstance(pages, list) or any(not isinstance(page, int) for page in pages):
        raise ContractError(f"chunk {chunk_id}.page_refs 必须是整数数组")
    return Chunk(
        chunk_id=chunk_id,
        item_ids=_strings(value.get("item_ids", []), f"chunk {chunk_id}.item_ids"),
        text=str(value.get("text") or ""),
        breadcrumb=str(value.get("breadcrumb") or ""),
        modalities=_strings(value.get("modalities", []), f"chunk {chunk_id}.modalities"),
        asset_ids=_strings(value.get("asset_ids", []), f"chunk {chunk_id}.asset_ids"),
        page_refs=list(dict.fromkeys(pages)),
        provenance=_object(value.get("provenance", {}), f"chunk {chunk_id}.provenance"),
        quality=_object(value.get("quality", {}), f"chunk {chunk_id}.quality"),
    )


def load_package(package_path: str | Path) -> Package:
    root = Path(package_path).expanduser().resolve()
    if not root.is_dir():
        raise ContractError(f"package 目录不存在：{root}")
    manifest_path = _safe_path(root, "manifest.json", "manifest")
    manifest = _object(_read_json(manifest_path, "manifest.json"), "manifest.json")
    schema_version = str(manifest.get("schema_version") or "")
    if schema_version != SUPPORTED_SCHEMA:
        raise ContractError(
            f"暂不支持 schema_version={schema_version!r}，当前要求 {SUPPORTED_SCHEMA}"
        )
    package_id = str(manifest.get("package_id") or "").strip()
    if not package_id or any(char in package_id for char in "/\\@# "):
        raise ContractError("package_id 不能为空，且不能包含路径符号、空格、@ 或 #")
    document = _object(manifest.get("document", {}), "manifest.document")
    source = _object(document.get("source", {}), "manifest.document.source")
    parser = _object(manifest.get("parser", {}), "manifest.parser")
    artifacts = _object(manifest.get("artifacts", {}), "manifest.artifacts")
    items_path = _safe_path(root, str(artifacts.get("items") or ""), "artifacts.items")
    chunks_path = _safe_path(root, str(artifacts.get("chunks") or ""), "artifacts.chunks")
    assets_index_path = _safe_path(
        root, str(artifacts.get("assets_index") or ""), "artifacts.assets_index"
    )

    item_values = _read_jsonl(items_path, "items.jsonl")
    chunk_values = _read_jsonl(chunks_path, "chunks.jsonl")
    asset_values = _read_json(assets_index_path, "assets.json")
    if isinstance(asset_values, dict):
        asset_values = asset_values.get("assets", [])
    if not isinstance(asset_values, list):
        raise ContractError("assets.json 必须是数组，或包含 assets 数组")

    items = [_item(value) for value in item_values]
    chunks = [_chunk(value) for value in chunk_values]
    assets_list = [_asset(_object(value, "asset record"), root) for value in asset_values]
    if len({item.item_id for item in items}) != len(items):
        raise ContractError("items.jsonl 存在重复 item_id")
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ContractError("chunks.jsonl 存在重复 chunk_id")
    if len({asset.asset_id for asset in assets_list}) != len(assets_list):
        raise ContractError("assets.json 存在重复 asset_id")
    item_ids = {item.item_id for item in items}
    asset_ids = {asset.asset_id for asset in assets_list}
    for item in items:
        missing = set(item.asset_ids) - asset_ids
        if missing:
            raise ContractError(f"item {item.item_id} 引用了不存在的 asset：{sorted(missing)}")
    for chunk in chunks:
        missing_items = set(chunk.item_ids) - item_ids
        missing_assets = set(chunk.asset_ids) - asset_ids
        if missing_items:
            raise ContractError(
                f"chunk {chunk.chunk_id} 引用了不存在的 item：{sorted(missing_items)}"
            )
        if missing_assets:
            raise ContractError(
                f"chunk {chunk.chunk_id} 引用了不存在的 asset：{sorted(missing_assets)}"
            )

    digest = hashlib.sha256()
    for path in (manifest_path, items_path, chunks_path, assets_index_path):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    for asset in sorted(assets_list, key=lambda value: value.asset_id):
        digest.update(asset.path.encode("utf-8"))
        digest.update((root / asset.path).read_bytes())

    return Package(
        package_path=str(root),
        package_id=package_id,
        schema_version=schema_version,
        title=str(document.get("title") or package_id),
        source_filename=str(source.get("filename") or ""),
        source_media_type=str(source.get("media_type") or "application/octet-stream"),
        parser_name=str(parser.get("name") or "unknown"),
        parser_version=str(parser.get("version") or "unknown"),
        items=sorted(items, key=lambda value: (value.sequence, value.item_id)),
        chunks=chunks,
        assets={asset.asset_id: asset for asset in assets_list},
        manifest=manifest,
        checksum=digest.hexdigest(),
    )


def validate_package(package_path: str | Path) -> dict[str, Any]:
    package = load_package(package_path)
    return {
        "status": "valid",
        "schema_version": package.schema_version,
        "package_id": package.package_id,
        "title": package.title,
        "checksum": package.checksum,
        "counts": {
            "items": len(package.items),
            "chunks": len(package.chunks),
            "assets": len(package.assets),
        },
        "modalities": sorted({item.item_type for item in package.items}),
    }
