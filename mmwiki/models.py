from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Asset:
    asset_id: str
    path: str
    media_type: str
    sha256: str = ""
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Item:
    item_id: str
    sequence: int
    item_type: str
    page_start: int | None
    page_end: int | None
    bbox: dict[str, Any]
    breadcrumb: str
    raw_text: str
    caption: str
    search_text: str
    table: dict[str, Any] | None
    equation: dict[str, Any] | None
    semantic: dict[str, Any]
    asset_ids: list[str]
    provenance: dict[str, Any]
    quality: dict[str, Any]
    searchable: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    item_ids: list[str]
    text: str
    breadcrumb: str
    modalities: list[str]
    asset_ids: list[str]
    page_refs: list[int]
    provenance: dict[str, Any]
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Package:
    package_path: str
    package_id: str
    schema_version: str
    title: str
    source_filename: str
    source_media_type: str
    parser_name: str
    parser_version: str
    items: list[Item]
    chunks: list[Chunk]
    assets: dict[str, Asset]
    manifest: dict[str, Any]
    checksum: str


@dataclass(frozen=True)
class SearchHit:
    source_id: str
    chunk_id: str
    title: str
    score: float
    snippet: str
    item_ids: list[str]
    modalities: list[str]
    asset_paths: list[str]
    pages: list[int]
    path: str
    wiki_paths: list[str] = field(default_factory=list)
    retrieval_channels: list[str] = field(default_factory=list)
    score_breakdown: dict[str, float] = field(default_factory=dict)
    matched_asset_id: str = ""
    matched_asset_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
