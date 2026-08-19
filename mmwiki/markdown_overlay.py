"""Create a read-only derived Markdown view for an existing local Wiki."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote

from .contracts import load_package


STANDARD_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)]+)\)"
)
OBSIDIAN_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
IMAGE_SUFFIXES = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _safe_image_path(page_path: Path, raw_path: str, wiki_root: Path) -> Path:
    candidate = unquote(raw_path.strip()).replace("\\", "/")
    if not candidate or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate):
        raise ValueError("remote or empty image path is not supported")
    if candidate.startswith(("/", "~")) or Path(candidate).is_absolute():
        raise ValueError("absolute image path is not supported")
    resolved = (page_path.parent / Path(candidate)).resolve()
    root = wiki_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("image path escapes Wiki root")
    return resolved


def _caption_entry(caption_map: Mapping[str, Any], sha256: str) -> dict[str, Any]:
    value = caption_map.get(sha256, {})
    if isinstance(value, str):
        return {"caption": value}
    return dict(value) if isinstance(value, Mapping) else {}


def caption_map_from_package(package: Any) -> dict[str, dict[str, str]]:
    """Build ``sha256 -> caption`` records from a MinerU Wiki package."""

    assets = getattr(package, "assets", {}) or {}
    result: dict[str, dict[str, str]] = {}
    for item in getattr(package, "items", []) or []:
        caption = str(
            getattr(item, "caption", "")
            or getattr(getattr(item, "content", None), "caption", "")
            or ""
        ).strip()
        if not caption:
            continue
        for asset_id in getattr(item, "asset_ids", []) or []:
            asset = assets.get(asset_id)
            sha256 = str(getattr(asset, "sha256", "") or "").strip()
            if sha256 and sha256 not in result:
                result[sha256] = {"asset_id": asset_id, "caption": caption}
    return result


def load_caption_map(package_path: Path) -> dict[str, dict[str, str]]:
    return caption_map_from_package(load_package(Path(package_path)))


def _asset_link(page_output: Path, assets_output: Path, asset_path: Path) -> str:
    relative = os.path.relpath(asset_path, page_output.parent)
    return PurePosixPath(relative).as_posix()


def _asset_record(
    *,
    digest: str,
    asset_id: str,
    source_path: Path,
    derived_path: Path,
    entry: Mapping[str, Any],
    original_alt: str,
) -> dict[str, Any]:
    caption = str(entry.get("caption", "") or "").strip()
    return {
        "asset_id": asset_id,
        "sha256": digest,
        "source_path": str(source_path),
        "derived_path": str(derived_path),
        "media_type": mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
        "caption": caption,
        "caption_source": "mineru" if caption else ("original_alt" if original_alt else "missing"),
        "status": "ready" if caption else "caption_missing",
        "caption_provenance": "mineru" if caption else ("original_alt" if original_alt else "missing"),
        "caption_status": "ready" if caption else "caption_missing",
    }


def materialize_wiki(
    wiki_root: Path,
    output_root: Path,
    caption_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy Markdown and referenced local images into a derived Wiki view."""

    wiki_root = Path(wiki_root).expanduser().resolve()
    output_root = Path(output_root).expanduser().resolve()
    if not wiki_root.is_dir():
        raise ValueError(f"Wiki root is not a directory: {wiki_root}")
    caption_map = caption_map or {}
    pages_output = output_root / "pages"
    assets_output = output_root / "assets"
    pages_output.mkdir(parents=True, exist_ok=True)
    assets_output.mkdir(parents=True, exist_ok=True)

    assets: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    copied = 0

    def rewrite(match: re.Match[str], page_path: Path, page_output: Path, kind: str) -> str:
        nonlocal copied
        if kind == "standard":
            original_alt, raw_path = match.group(1), match.group(2)
            title = re.match(r"^(.*?)(?:\s+[\"'].*[\"'])$", raw_path.strip())
            raw_path = title.group(1) if title else raw_path.strip()
        else:
            raw_path = match.group(1).strip()
            original_alt = ""
        try:
            source_path = _safe_image_path(page_path, raw_path, wiki_root)
            if source_path.suffix.lower() not in IMAGE_SUFFIXES:
                raise ValueError("file is not a supported local image")
            if not source_path.is_file():
                raise ValueError("image file does not exist")
        except ValueError as exc:
            errors.append({"page": str(page_path.relative_to(wiki_root)), "path": raw_path, "error": str(exc)})
            return match.group(0)

        digest = _digest(source_path)
        entry = _caption_entry(caption_map, digest)
        asset_id = str(entry.get("asset_id") or f"asset-{digest[:20]}")
        suffix = source_path.suffix.lower()
        derived_asset = assets_output / f"{asset_id}{suffix}"
        if digest not in assets:
            if not derived_asset.exists():
                shutil.copy2(source_path, derived_asset)
                copied += 1
            assets[digest] = _asset_record(
                digest=digest,
                asset_id=asset_id,
                source_path=source_path,
                derived_path=derived_asset,
                entry=entry,
                original_alt=original_alt,
            )
        alt = str(entry.get("caption") or original_alt or "")
        safe_alt = alt.replace("]", "\\]")
        return f"![{safe_alt}]({_asset_link(page_output, assets_output, derived_asset)})"

    for page_path in sorted(wiki_root.rglob("*.md")):
        if output_root == page_path or output_root in page_path.parents:
            continue
        relative = page_path.relative_to(wiki_root)
        page_output = pages_output / relative
        page_output.parent.mkdir(parents=True, exist_ok=True)
        content = page_path.read_text(encoding="utf-8")
        content = STANDARD_IMAGE_RE.sub(lambda m: rewrite(m, page_path, page_output, "standard"), content)
        content = OBSIDIAN_IMAGE_RE.sub(lambda m: rewrite(m, page_path, page_output, "obsidian"), content)
        page_output.write_text(content, encoding="utf-8")
        files.append(
            {
                "source_path": str(page_path),
                "derived_path": str(page_output),
                "sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
            }
        )

    manifest = {
        "schema_version": "external-wiki-v1",
        "wiki_id": output_root.name,
        "source_root": str(wiki_root),
        "pages": files,
        "assets": list(assets.values()),
        "errors": errors,
        "assets_copied": copied,
    }
    version_payload = json.dumps(
        {"pages": files, "assets": manifest["assets"]},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    manifest["source_version"] = hashlib.sha256(version_payload).hexdigest()
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
