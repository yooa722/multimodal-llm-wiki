from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any


AUXILIARY = {"page_header", "page_footer", "page_number", "page_aside_text"}


def inline_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(inline_text(item) for item in value).strip()
    if isinstance(value, dict):
        if isinstance(value.get("content"), str):
            return value["content"].strip()
        return inline_text(value.get("children"))
    return ""


def strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return [value.strip()] if isinstance(value, str) and value.strip() else []
    return [text for item in value if (text := inline_text(item))]


def is_searchable_item(kind: str, search_text: str, asset_ids: list[str]) -> bool:
    if kind in AUXILIARY:
        return False
    return bool(search_text.strip()) or (
        kind in {"image", "chart", "table"} and bool(asset_ids)
    )


def clean_html(value: str) -> str:
    return " ".join(
        html_module.unescape(re.sub(r"<[^>]+>", " ", value)).split()
    )


def rows_from_html(value: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", value, re.I | re.S):
        cells = [
            clean_html(cell)
            for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, re.I | re.S)
        ]
        if cells:
            rows.append(cells)
    return rows


def safe_id(value: str) -> str:
    result = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-_")
    return result or "document"


def locate_asset(source_root: Path, content: dict[str, Any]) -> Path | None:
    source = content.get("image_source")
    raw = source.get("path") if isinstance(source, dict) else None
    raw = raw or content.get("img_path") or content.get("image_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"资源路径越界：{raw}")
    target = (source_root / relative).resolve()
    if source_root.resolve() not in target.parents or not target.is_file():
        raise ValueError(f"资源不存在：{raw}")
    return target


def convert(source: Path, output_root: Path) -> Path:
    candidates = sorted(source.glob("*_content_list_v2.json"))
    if len(candidates) != 1:
        raise ValueError(f"{source} 必须包含一份 *_content_list_v2.json")
    pages = json.loads(candidates[0].read_text(encoding="utf-8"))
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("content_list_v2 顶层必须是页面数组")
    original_name = source.name.split(".pdf-", 1)[0] + ".pdf"
    package_id = safe_id(Path(original_name).stem)
    target = output_root / package_id
    if target.exists():
        raise ValueError(f"目标 package 已存在，请换一个空输出目录：{target}")
    (target / "assets").mkdir(parents=True)
    (target / "raw").mkdir(parents=True)
    shutil.copy2(candidates[0], target / "raw" / candidates[0].name)

    items: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    assets: dict[str, dict[str, Any]] = {}
    headings: list[str] = []
    sequence = 0
    for page_index, page in enumerate(pages, 1):
        for raw_index, raw in enumerate(page, 1):
            if not isinstance(raw, dict):
                continue
            sequence += 1
            kind = str(raw.get("type") or "text")
            content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
            if kind == "title":
                text = inline_text(content.get("title_content"))
                level = max(1, min(int(content.get("level", 1)), 6))
                headings[:] = headings[: level - 1]
                if text:
                    headings.append(text)
            elif kind == "paragraph":
                text = inline_text(content.get("paragraph_content"))
            elif kind == "equation_interline":
                text = inline_text(content.get("math_content"))
            else:
                text = inline_text(content.get("content"))
            caption_keys = {
                "image": ("image_caption",),
                "chart": ("chart_caption",),
                "table": ("table_caption",),
            }.get(kind, ())
            caption = " ".join(
                value for key in caption_keys for value in strings(content.get(key))
            )
            table_html = str(content.get("html") or "") if kind == "table" else ""
            raw_text = text or caption
            search_parts = [" > ".join(headings), raw_text]
            if table_html:
                table_rows = rows_from_html(table_html)
                labels = [cell for row in table_rows[:8] for cell in row[:8]]
                search_parts.extend(labels)
            else:
                table_rows = []
            item_id = f"item-p{page_index:04d}-b{raw_index:04d}"
            asset_ids: list[str] = []
            asset_source = locate_asset(source, content)
            if asset_source:
                digest = hashlib.sha256(asset_source.read_bytes()).hexdigest()
                asset_id = f"asset-{digest[:20]}"
                suffix = asset_source.suffix.lower() or ".bin"
                asset_relative = f"assets/{digest[:20]}{suffix}"
                asset_target = target / asset_relative
                if not asset_target.exists():
                    shutil.copy2(asset_source, asset_target)
                assets[asset_id] = {
                    "asset_id": asset_id,
                    "path": asset_relative,
                    "media_type": mimetypes.guess_type(asset_target.name)[0]
                    or "application/octet-stream",
                    "sha256": digest,
                    "source_path": asset_source.relative_to(source).as_posix(),
                }
                asset_ids.append(asset_id)
            search_text = " ".join(part for part in search_parts if part).strip()
            searchable = is_searchable_item(kind, search_text, asset_ids)
            item = {
                "item_id": item_id,
                "sequence": sequence,
                "type": "equation" if kind == "equation_interline" else kind,
                "raw_type": kind,
                "page_start": page_index,
                "page_end": page_index,
                "bbox": {
                    "values": raw.get("bbox", []),
                    "coordinate_system": "normalized_1000",
                    "origin": "top_left",
                },
                "breadcrumb": " > ".join(headings),
                "content": {
                    "raw_text": raw_text,
                    "caption": caption,
                    "search_text": search_text,
                    "semantic": {},
                },
                "assets": [{"asset_id": value} for value in asset_ids],
                "provenance": {
                    "parser": "mineru",
                    "raw_ref": f"content_list_v2[{page_index - 1}][{raw_index - 1}]",
                    "page": page_index,
                },
                "quality": {"needs_review": False, "review_reasons": []},
                "retrieval": {"searchable": searchable, "exclude": not searchable},
            }
            if table_html:
                item["content"]["table"] = {"html": table_html, "rows": table_rows}
            if kind == "equation_interline":
                item["content"]["equation"] = {"latex": text, "math_type": "latex"}
            items.append(item)
            if searchable:
                chunks.append(
                    {
                        "chunk_id": f"chunk-{package_id}-{sequence:05d}",
                        "item_ids": [item_id],
                        "text": item["content"]["search_text"],
                        "breadcrumb": item["breadcrumb"],
                        "modalities": [item["type"]],
                        "asset_ids": asset_ids,
                        "page_refs": [page_index],
                        "provenance": {"item_ids": [item_id], "pages": [page_index]},
                        "quality": {"needs_review": False},
                    }
                )

    manifest = {
        "schema_version": "mmwiki-0.1",
        "package_id": package_id,
        "document": {
            "title": Path(original_name).stem,
            "source": {
                "filename": original_name,
                "structured_output": f"raw/{candidates[0].name}",
                "media_type": "application/pdf",
            },
        },
        "parser": {
            "name": "mineru",
            "version": "reference-data",
            "structured_output": "content_list_v2",
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
            "semantic_enrichment": "none",
            "retrieval_proxy": "rule-generated-reference-only",
        },
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for name, values in (("items.jsonl", items), ("chunks.jsonl", chunks)):
        (target / name).write_text(
            "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
            encoding="utf-8",
        )
    (target / "assets.json").write_text(
        json.dumps(list(assets.values()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="仅用于把本次既有 MinerU 数据转成临时 mmwiki-0.1 package"
    )
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    sources = sorted(
        {
            path.parent
            for path in args.source_root.rglob("*_content_list_v2.json")
            if path.is_file()
        }
    )
    if not sources:
        raise ValueError(f"没有找到 *_content_list_v2.json：{args.source_root}")
    outputs = [str(convert(source, args.output_root)) for source in sources]
    print(json.dumps({"packages": outputs, "count": len(outputs)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
