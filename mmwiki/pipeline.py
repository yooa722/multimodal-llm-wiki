from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import load_package, validate_package
from .config import FeatureConfig, load_feature_config, resolve_query_mode
from .markdown_overlay import load_caption_map, materialize_wiki
from .models import Item, Package
from .provider import (
    OpenAICompatibleProvider,
    QUERY_REWRITE_PROMPT_VERSION,
    VISION_PROMPT_VERSION,
    WIKI_PROMPT_VERSION,
    validate_wiki_analysis,
    validate_wiki_compilation,
)
from .retrieval import (
    BailianRetrievalProvider,
    HybridRetriever,
    RETRIEVAL_MODES,
    RetrievalIndex,
)
from .search import Retriever, navigate_wiki


class PipelineError(RuntimeError):
    pass


NON_WIKI_VAULT_DOCUMENTS = (
    "Demo-Questions.md",
    "Demo验收记录.md",
    "Form 7004 Filing Requirements and Codes.md",
    "Tentative Total Tax.md",
    "Wiki组技术方案.md",
    "周五阶段总结.md",
    "增强检索验收结果.md",
    "多模态Wiki框架调研.md",
    "未命名.base",
    "首轮测试结果.md",
)

VISUAL_ITEM_TYPES = {"image", "figure", "chart"}
RICH_EVIDENCE_ITEM_TYPES = VISUAL_ITEM_TYPES | {"table", "equation"}
INGEST_STAGES = ("all", "text", "multimodal")
MANAGED_EVIDENCE_START = "<!-- mmwiki:multimodal-evidence:start -->"
MANAGED_EVIDENCE_END = "<!-- mmwiki:multimodal-evidence:end -->"
WIKILINK_PATTERN = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    result = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-_")
    return result or "source"


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _bounded_env_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(lower, min(value, upper))


def _merge_usage(*records: dict[str, Any] | list[dict[str, Any]]) -> dict[str, int]:
    """Merge numeric provider usage without assuming one vendor's token keys."""
    totals: dict[str, int] = {}
    pending: list[Any] = list(records)
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        for key, item in value.items():
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                totals[str(key)] = totals.get(str(key), 0) + int(item)
            elif isinstance(item, (dict, list)):
                pending.append(item)
    return totals


def _directory_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(
        target.stat().st_size
        for target in path.rglob("*")
        if target.is_file()
    )


def _is_multimodal_item(item: Item) -> bool:
    return bool(
        item.item_type in RICH_EVIDENCE_ITEM_TYPES
        or item.table
        or item.equation
        or item.asset_ids
    )


def _text_projection_item(item: Item) -> dict[str, Any]:
    """Keep a text proxy while withholding first-class table/image/equation data."""
    value = item.to_dict()
    metadata = dict(value.get("metadata") or {})
    metadata.update(
        {
            "representation": "text-proxy",
            "source_item_type": item.item_type,
        }
    )
    value.update(
        {
            "item_type": "text" if _is_multimodal_item(item) else item.item_type,
            "table": None,
            "equation": None,
            "asset_ids": [],
            "semantic": {},
            "metadata": metadata,
        }
    )
    return value


def _text_projection_chunk(chunk: Any) -> dict[str, Any]:
    value = chunk.to_dict()
    value["modalities"] = ["text"]
    value["asset_ids"] = []
    return value


def _table_markdown(table: dict[str, Any] | None, limit: int = 12000) -> str:
    if not table:
        return ""
    rows = table.get("rows")
    if isinstance(rows, list) and rows and all(isinstance(row, list) for row in rows):
        width = max(len(row) for row in rows)
        normalized = [[str(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(normalized[0]) + " |",
            "| " + " | ".join(["---"] * width) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
        return "\n".join(lines)[:limit]
    html = str(table.get("html") or "")
    return html[:limit]


def _clean_generated_content(content: str, title: str) -> str:
    value = content.strip()
    while value.startswith("---\n"):
        closing = value.find("\n---", 4)
        if closing >= 0:
            value = value[closing + 4 :].lstrip()
        else:
            break
    value = re.sub(r"(?ms)^---\n.*?^---\n?", "", value).strip()
    if MANAGED_EVIDENCE_START in value:
        value = value.split(MANAGED_EVIDENCE_START, 1)[0].rstrip()
    evidence_heading = value.find("\n## Evidence\n")
    if evidence_heading >= 0:
        value = value[:evidence_heading].rstrip()
    lines = value.splitlines()
    while lines and (
        not lines[0].strip()
        or lines[0].strip().casefold() == f"# {title}".casefold()
    ):
        lines = lines[1:]
    value = "\n".join(lines).strip()
    return re.sub(
        r"\[\[([^\[\]\s]+@[^#\[\]]+#[^\[\]]+)\]\]",
        r"`\1`",
        value,
    )


class WikiPipeline:
    def __init__(
        self,
        project_root: str | Path,
        feature_config: FeatureConfig | None = None,
    ):
        self._state_lock = threading.RLock()
        self.root = Path(project_root).expanduser().resolve()
        self.runtime = self.root / "runtime"
        self.vault = self.runtime / "vault"
        self.state_path = self.runtime / "state.json"
        self.query_path = self.runtime / "queries.jsonl"
        self.curation_log_path = self.runtime / "curation-log.jsonl"
        self.retrieval_index_path = self.runtime / "retrieval-index.json"
        self.raw_root = self.runtime / "raw"
        self.build_cache_root = self.runtime / "build-cache"
        self.asset_root = self.vault / "assets"
        self.wiki_root = self.vault / "wiki"
        self.purpose_path = self.vault / "wiki-purpose.md"
        self.schema_path = self.vault / "schema.md"
        self.index_path = self.wiki_root / "index.md"
        self.overview_path = self.wiki_root / "overview.md"
        self.log_path = self.wiki_root / "log.md"
        self.graph_path = self.wiki_root / "graph.json"
        self.graph_report_path = self.wiki_root / "graph-health.md"
        self.maintenance_path = self.wiki_root / "maintenance.md"
        self.features = feature_config or load_feature_config(self.root)
        self.ensure_layout()

    def configure_features(
        self,
        *,
        vlm: str | None = None,
        vector_retrieval: str | None = None,
    ) -> FeatureConfig:
        self.features = load_feature_config(
            self.root,
            vlm=vlm,
            vector_retrieval=vector_retrieval,
        )
        return self.features

    def ensure_layout(self) -> None:
        for path in (
            self.runtime,
            self.raw_root,
            self.build_cache_root,
            self.asset_root,
            self.wiki_root / "sources",
            self.wiki_root / "concepts",
            self.wiki_root / "entities",
            self.wiki_root / "analyses",
            self.wiki_root / "evidence",
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.state_path.is_file():
            self._save_state(
                {"schema_version": "0.4", "sources": {}, "pages": {}, "queries": []}
            )
        if not self.purpose_path.is_file():
            purpose_template = self.root / "config" / "purpose.md"
            if purpose_template.is_file():
                shutil.copy2(purpose_template, self.purpose_path)
            else:
                self.purpose_path.write_text(
                    "# Wiki Purpose\n\n持久编译来源知识，并保持事实可追溯。\n",
                    encoding="utf-8",
                )
        if not self.schema_path.is_file():
            template = self.root / "config" / "schema.md"
            if template.is_file():
                shutil.copy2(template, self.schema_path)
            else:
                self.schema_path.write_text(
                    "# Wiki Schema\n\n来源只读；知识页必须记录来源和 Evidence。\n",
                    encoding="utf-8",
                )
        if not self.log_path.is_file():
            self.log_path.write_text("# Wiki Log\n\n", encoding="utf-8")
        if os.environ.get("MMWIKI_INSTALL_OBSIDIAN_PLUGIN", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            self._install_obsidian_plugin()
        self._remove_non_wiki_vault_documents()
        state = self._load_state()
        changed = state.get("schema_version") != "0.4"
        state["schema_version"] = "0.4"
        for source_id, source in state.get("sources", {}).items():
            if source.get("source_type") == "external_markdown":
                continue
            evidence_map_path = f"wiki/evidence/{slugify(str(source_id))}-multimodal.md"
            if source.get("evidence_map_path") != evidence_map_path:
                source["evidence_map_path"] = evidence_map_path
                changed = True
        if changed:
            self._save_state(state)
        self._write_navigation(state)

    def _wiki_instructions(self) -> str:
        purpose = (
            self.purpose_path.read_text(encoding="utf-8")
            if self.purpose_path.is_file()
            else ""
        )
        schema = (
            self.schema_path.read_text(encoding="utf-8")
            if self.schema_path.is_file()
            else ""
        )
        return f"# Wiki Purpose\n\n{purpose}\n\n# Wiki Schema\n\n{schema}".strip()

    def _log(self, operation: str, detail: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._state_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"## [{stamp}] {operation} | {detail}\n\n")

    def _install_obsidian_plugin(self) -> None:
        source = self.root / "obsidian-plugin"
        if not source.is_dir():
            return
        target = self.vault / ".obsidian" / "plugins" / "multimodal-wiki-query"
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("main.js", "manifest.json", "styles.css"):
            if (source / filename).is_file():
                shutil.copy2(source / filename, target / filename)
        plugins = self.vault / ".obsidian" / "community-plugins.json"
        plugins.parent.mkdir(parents=True, exist_ok=True)
        enabled = json.loads(plugins.read_text(encoding="utf-8")) if plugins.is_file() else []
        if "multimodal-wiki-query" not in enabled:
            enabled.append("multimodal-wiki-query")
        plugins.write_text(json.dumps(enabled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _remove_non_wiki_vault_documents(self) -> None:
        for filename in NON_WIKI_VAULT_DOCUMENTS:
            path = self.vault / filename
            if path.is_file():
                path.unlink()

    def _load_state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._state_lock:
            _atomic_write_text(
                self.state_path,
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            )

    @staticmethod
    def _wiki_coverage(state: dict[str, Any]) -> dict[str, Any]:
        source_ids = {str(value) for value in state.get("sources", {})}
        covered = {
            str(source_id)
            for page in state.get("pages", {}).values()
            for source_id in page.get("source_ids", [])
            if str(source_id) in source_ids
        }
        uncovered = sorted(source_ids - covered)
        return {
            "source_pages": len(source_ids),
            "stable_pages": len(state.get("pages", {})),
            "stable_page_source_coverage": len(covered),
            "stable_page_source_total": len(source_ids),
            "stable_page_source_coverage_rate": (
                round(len(covered) / len(source_ids), 4) if source_ids else 1.0
            ),
            "uncovered_source_ids": uncovered,
        }

    def validate(self, package_path: str | Path) -> dict[str, Any]:
        return validate_package(package_path)

    def _copy_assets(
        self,
        package: Package,
        asset_ids: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        package_root = Path(package.package_path)
        target_root = self.asset_root / slugify(package.package_id)
        target_root.mkdir(parents=True, exist_ok=True)
        for asset in package.assets.values():
            if asset_ids is not None and asset.asset_id not in asset_ids:
                continue
            source = (package_root / asset.path).resolve()
            suffix = source.suffix.lower()
            target = target_root / f"{slugify(asset.asset_id)}{suffix}"
            shutil.copy2(source, target)
            result[asset.asset_id] = {
                **asset.to_dict(),
                "vault_path": target.relative_to(self.vault).as_posix(),
            }
        return result

    @staticmethod
    def _item_evidence_id(package: Package, item_id: str) -> str:
        return f"{package.package_id}@{package.checksum[:12]}#{item_id}"

    def _render_item(self, package: Package, item: Item, assets: dict[str, dict[str, Any]]) -> str:
        evidence_id = self._item_evidence_id(package, item.item_id)
        lines = [f"<a id=\"{slugify(item.item_id)}\"></a>", f"### {item.item_id}", ""]
        labels = [item.item_type]
        if item.page_start is not None:
            labels.append(f"第 {item.page_start} 页")
        lines.append(" · ".join(labels))
        lines.append("")
        text = item.raw_text or item.caption or item.search_text
        if text:
            lines.extend([text, ""])
        table = _table_markdown(item.table)
        if table:
            lines.extend([table, ""])
        if item.equation and item.equation.get("latex"):
            lines.extend([f"$${item.equation['latex']}$$", ""])
        description = str(item.semantic.get("description") or "")
        if description and description not in text:
            lines.extend([f"> 语义说明：{description}", ""])
        for asset_id in item.asset_ids:
            vault_path = assets[asset_id]["vault_path"]
            lines.extend([f"![[{vault_path}]]", ""])
        if item.quality.get("needs_review"):
            lines.extend(["> [!warning] 解析组标记此记录需要复核。", ""])
        lines.extend([f"Evidence ID: `{evidence_id}`", ""])
        return "\n".join(lines)

    def _baseline_page(
        self,
        package: Package,
        assets: dict[str, dict[str, Any]],
        item_ids: set[str] | None = None,
        text_only: bool = False,
    ) -> str:
        items = [
            item
            for item in package.items
            if item_ids is None or item.item_id in item_ids
        ]
        active_item_ids = {item.item_id for item in items}
        chunks = [
            chunk
            for chunk in package.chunks
            if active_item_ids.intersection(chunk.item_ids)
        ]
        modalities = ["text"] if text_only else sorted({item.item_type for item in items})
        frontmatter = (
            "---\n"
            f"package_id: {yaml_string(package.package_id)}\n"
            f"source_version: {yaml_string(package.checksum[:12])}\n"
            f"source_filename: {yaml_string(package.source_filename)}\n"
            f"modalities: {json.dumps(modalities, ensure_ascii=False)}\n"
            "---\n"
        )
        if text_only:
            blocks = []
            for item in items:
                evidence_id = self._item_evidence_id(package, item.item_id)
                text = item.raw_text or item.caption or item.search_text
                source_type = item.item_type
                label = "text" if not _is_multimodal_item(item) else f"text-proxy({source_type})"
                lines = [
                    f'<a id="{slugify(item.item_id)}"></a>',
                    f"### {item.item_id}",
                    "",
                    " · ".join(
                        value
                        for value in (
                            label,
                            f"第 {item.page_start} 页" if item.page_start is not None else "",
                        )
                        if value
                    ),
                    "",
                ]
                if text:
                    lines.extend([text, ""])
                lines.extend([f"Evidence ID: `{evidence_id}`", ""])
                blocks.append("\n".join(lines))
        else:
            blocks = [self._render_item(package, item, assets) for item in items]
        return (
            f"{frontmatter}\n# {package.title}\n\n"
            f"- Package：`{package.package_id}`\n"
            f"- Parser：`{package.parser_name} {package.parser_version}`\n"
            f"- Items：{len(items)}\n"
            f"- Chunks：{len(chunks)}\n"
            f"- Assets：{len(assets)}\n\n"
            "## 原始事实\n\n" + "\n".join(blocks)
        )

    def _builder_evidence(
        self,
        package: Package,
        item_ids: set[str] | None = None,
        text_only: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in package.items:
            if not item.searchable or (
                item_ids is not None and item.item_id not in item_ids
            ):
                continue
            content = item.raw_text or item.caption or item.search_text
            if item.table and not text_only:
                content += "\n" + _table_markdown(item.table, 5000)
            result.append(
                {
                    "id": self._item_evidence_id(package, item.item_id),
                    "type": (
                        "text-proxy"
                        if text_only and _is_multimodal_item(item)
                        else item.item_type
                    ),
                    "section": item.breadcrumb,
                    "page": item.page_start,
                    "raw_text": item.raw_text[:6000],
                    "caption": item.caption[:2000],
                    "search_text": item.search_text[:6000],
                    "semantic_description": (
                        ""
                        if text_only
                        else str(item.semantic.get("description") or "")[:3000]
                    ),
                    "text": content[:6000],
                    "bbox": item.bbox,
                    "asset_ids": [] if text_only else item.asset_ids,
                    "quality": item.quality,
                }
            )
        return result

    def _builder_image_payloads(
        self,
        package: Package,
        assets: dict[str, dict[str, Any]],
        evidence_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, str]], dict[str, Any]]:
        effective_limit = limit or _bounded_env_int(
            "MMWIKI_MAX_BUILD_IMAGES", 8, 1, 16
        )
        effective_limit = max(1, min(int(effective_limit), 32))
        payloads: list[dict[str, str]] = []
        used: set[str] = set()
        candidates = 0
        for item in package.items:
            if not item.searchable:
                continue
            evidence_id = self._item_evidence_id(package, item.item_id)
            if evidence_ids is not None and evidence_id not in evidence_ids:
                continue
            for asset_id in item.asset_ids:
                asset = assets.get(asset_id, {})
                vault_path = str(asset.get("vault_path") or "")
                target = (self.vault / vault_path).resolve()
                mime = str(
                    asset.get("media_type")
                    or mimetypes.guess_type(target.name)[0]
                    or ""
                )
                if (
                    not vault_path
                    or vault_path in used
                    or self.vault not in target.parents
                    or not target.is_file()
                    or not mime.startswith("image/")
                ):
                    continue
                candidates += 1
                used.add(vault_path)
                if len(payloads) >= effective_limit:
                    continue
                payloads.append(
                    {
                        "evidence_id": evidence_id,
                        "asset_id": str(asset_id),
                        "asset_path": vault_path,
                        "data_url": f"data:{mime};base64,"
                        + base64.b64encode(target.read_bytes()).decode("ascii"),
                    }
                )
        return payloads, {
            "candidate_images": candidates,
            "analyzed_images": len(payloads),
            "max_images": effective_limit,
            "truncated": candidates > len(payloads),
        }

    @staticmethod
    def _merge_wiki_analyses(
        analyses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {
            "summary": "\n".join(
                str(analysis.get("summary") or "").strip()
                for analysis in analyses
                if str(analysis.get("summary") or "").strip()
            ),
            "claims": [],
            "entities": [],
            "concepts": [],
            "contradictions": [],
            "page_actions": [],
        }
        for key in ("claims", "entities", "concepts", "contradictions"):
            seen: set[str] = set()
            for analysis in analyses:
                for record in analysis.get(key, []):
                    identity = json.dumps(record, ensure_ascii=False, sort_keys=True)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    merged[key].append(record)
        actions: dict[tuple[str, str], dict[str, Any]] = {}
        for analysis in analyses:
            for record in analysis.get("page_actions", []):
                identity = (
                    str(record.get("title") or "").strip().casefold(),
                    str(record.get("kind") or "").strip(),
                )
                if identity not in actions:
                    actions[identity] = dict(record)
                    continue
                previous = actions[identity]
                reasons = list(
                    dict.fromkeys(
                        filter(
                            None,
                            [
                                str(previous.get("reason") or "").strip(),
                                str(record.get("reason") or "").strip(),
                            ],
                        )
                    )
                )
                previous["reason"] = "；".join(reasons)
                if record.get("action") == "update":
                    previous["action"] = "update"
        merged["page_actions"] = list(actions.values())
        merged["batch_usage"] = [
            analysis.get("_usage", {}) for analysis in analyses
        ]
        return merged

    def _analyze_wiki_full_scale(
        self,
        package: Package,
        evidence: list[dict[str, Any]],
        assets: dict[str, dict[str, Any]],
        state: dict[str, Any],
        schema: str,
        llm: OpenAICompatibleProvider,
        vision_llm: OpenAICompatibleProvider,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        by_page: dict[int, list[dict[str, Any]]] = {}
        for record in evidence:
            page = int(record.get("page") or 0)
            by_page.setdefault(page, []).append(record)
        analyses: list[dict[str, Any]] = []
        batch_records: list[dict[str, Any]] = []
        total_candidates = 0
        total_analyzed = 0
        catalog = self._wiki_catalog(
            state, package.package_id, stage="multimodal"
        )
        cache_root = (
            self.build_cache_root
            / slugify(package.package_id)
            / package.checksum[:12]
            / slugify(WIKI_PROMPT_VERSION)
        )
        cache_root.mkdir(parents=True, exist_ok=True)
        for page, page_evidence in sorted(by_page.items()):
            allowed = {str(record["id"]) for record in page_evidence}
            images, image_stats = self._builder_image_payloads(
                package,
                assets,
                evidence_ids=allowed,
                limit=32,
            )
            if image_stats["candidate_images"] and not vision_llm.configured:
                raise PipelineError(
                    "完整多模态构建发现视觉资源，但视觉分析模型未配置"
                )
            if image_stats["truncated"]:
                raise PipelineError(
                    f"第 {page} 页视觉资源未完整进入分析，拒绝标记为 full-scale"
                )
            analyzer = vision_llm if images else llm
            cache_path = cache_root / f"page-{page:04d}-analysis.json"
            cached = False
            analysis: dict[str, Any]
            if cache_path.is_file():
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cache.get("model") == analyzer.model
                    and cache.get("evidence_ids") == sorted(allowed)
                    and cache.get("asset_paths")
                    == sorted(image["asset_path"] for image in images)
                ):
                    analysis = validate_wiki_analysis(
                        cache.get("analysis", {}), allowed
                    )
                    cached = True
                else:
                    analysis = analyzer.analyze_wiki(
                        f"{package.title} · 第 {page} 页",
                        page_evidence,
                        catalog,
                        schema,
                        images,
                        stage="multimodal",
                    )
            else:
                analysis = analyzer.analyze_wiki(
                    f"{package.title} · 第 {page} 页",
                    page_evidence,
                    catalog,
                    schema,
                    images,
                    stage="multimodal",
                )
            if not cached:
                _atomic_write_text(
                    cache_path,
                    json.dumps(
                        {
                            "model": analyzer.model,
                            "evidence_ids": sorted(allowed),
                            "asset_paths": sorted(
                                image["asset_path"] for image in images
                            ),
                            "analysis": analysis,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
            analyses.append(analysis)
            total_candidates += int(image_stats["candidate_images"])
            total_analyzed += int(image_stats["analyzed_images"])
            batch_records.append(
                {
                    "page": page,
                    "evidence_items": len(page_evidence),
                    "candidate_images": image_stats["candidate_images"],
                    "analyzed_images": image_stats["analyzed_images"],
                    "model": analyzer.model,
                    "cached": cached,
                }
            )
        merged = self._merge_wiki_analyses(analyses)
        models = list(dict.fromkeys(record["model"] for record in batch_records))
        return (
            merged,
            {
                "candidate_images": total_candidates,
                "analyzed_images": total_analyzed,
                "max_images": None,
                "truncated": False,
                "used_actual_images": total_analyzed > 0,
                "full_scale": True,
                "batch_count": len(batch_records),
                "batches": batch_records,
            },
            "+".join(models),
        )

    @staticmethod
    def _page_directory(kind: str) -> str:
        mapping = {
            "concept": "concepts",
            "entity": "entities",
            "analysis": "analyses",
            "comparison": "analyses",
            "source-summary": "analyses",
        }
        if kind not in mapping:
            raise PipelineError(f"Wiki Builder 返回了不支持的页面类型：{kind}")
        return mapping[kind]

    def _evidence_records(
        self,
        state: dict[str, Any],
        package: Package | None = None,
        package_assets: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for source_id, source in state.get("sources", {}).items():
            prefix = f"{source_id}@{source.get('source_version', '')}#"
            for item in source.get("items", []):
                item_id = str(item.get("item_id") or "")
                if not item_id:
                    continue
                records[prefix + item_id] = {
                    "item": item,
                    "source_path": str(source.get("wiki_path") or ""),
                    "assets": source.get("assets", {}),
                }
        if package is not None:
            source_path = f"wiki/sources/{slugify(package.package_id)}.md"
            for item in package.items:
                records[self._item_evidence_id(package, item.item_id)] = {
                    "item": item.to_dict(),
                    "source_path": source_path,
                    "assets": package_assets or {},
                }
        return records

    def _render_page_evidence(
        self,
        evidence_ids: list[str],
        records: dict[str, dict[str, Any]],
    ) -> tuple[str, list[str], list[str]]:
        modalities: list[str] = []
        visual_evidence_ids: list[str] = []
        rich_blocks: list[str] = []
        for evidence_id in evidence_ids:
            record = records.get(evidence_id)
            if record is None:
                continue
            item = record["item"]
            item_type = str(item.get("item_type") or "text")
            if item_type not in modalities:
                modalities.append(item_type)
            assets = record.get("assets", {})
            image_assets = []
            for asset_id in item.get("asset_ids", []):
                asset = assets.get(asset_id, {})
                vault_path = str(asset.get("vault_path") or "")
                mime = str(asset.get("media_type") or "")
                if vault_path and (mime.startswith("image/") or not mime):
                    image_assets.append(vault_path)
            is_rich = bool(
                item_type in RICH_EVIDENCE_ITEM_TYPES
                or item.get("table")
                or item.get("equation")
                or image_assets
            )
            if not is_rich:
                continue
            visual_evidence_ids.append(evidence_id)
            page = item.get("page_start")
            label = " · ".join(
                value
                for value in (
                    item_type,
                    f"第 {page} 页" if page is not None else "",
                    str(item.get("breadcrumb") or ""),
                )
                if value
            )
            lines = [f"### {label or item.get('item_id')}", ""]
            caption = str(item.get("caption") or "").strip()
            if caption:
                lines.extend([f"**原始 Caption：** {caption}", ""])
            description = str(
                (item.get("semantic") or {}).get("description") or ""
            ).strip()
            if description and description != caption:
                lines.extend(
                    [
                        "> [!note] 上游语义说明（派生信息，不替代原图）",
                        f"> {description}",
                        "",
                    ]
                )
            table = _table_markdown(item.get("table"), 6000)
            if table:
                lines.extend([table, ""])
            equation = item.get("equation") or {}
            if equation.get("latex"):
                lines.extend([f"$${equation['latex']}$$", ""])
            for vault_path in image_assets:
                lines.extend([f"![[{vault_path}]]", ""])
            source_path = str(record.get("source_path") or "").removesuffix(".md")
            item_id = str(item.get("item_id") or "")
            lines.extend(
                [
                    f"Evidence ID：`{evidence_id}`",
                    f"来源：[[{source_path}#{item_id}|打开原始 Evidence]]",
                ]
            )
            rich_blocks.append("\n".join(lines))

        citations = "\n".join(f"- `{value}`" for value in evidence_ids)
        parts = [
            MANAGED_EVIDENCE_START,
            "## Evidence",
            "",
            citations,
        ]
        if rich_blocks:
            parts.extend(
                [
                    "",
                    "## 可核验的多模态原始证据",
                    "",
                    "> 以下图片、表格和公式由 Pipeline 根据 Evidence ID 确定性回填；模型解释不能替代原始资源。",
                    "",
                    "\n\n".join(rich_blocks),
                ]
            )
        parts.extend([MANAGED_EVIDENCE_END, ""])
        return "\n".join(parts), sorted(modalities), visual_evidence_ids

    def _write_generated_pages(
        self,
        package: Package,
        plan: dict[str, Any],
        source_path: str,
        state: dict[str, Any],
        package_assets: dict[str, dict[str, Any]],
        stage: str = "curated",
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        allowed = {self._item_evidence_id(package, item.item_id) for item in package.items}
        evidence_records = self._evidence_records(state, package, package_assets)
        for index, page in enumerate(plan.get("pages", []), 1):
            title = str(page.get("title") or f"{package.title}-{index}")
            content = _clean_generated_content(str(page.get("content") or ""), title)
            refs = list(dict.fromkeys(str(value) for value in page.get("evidence_refs", [])))
            if set(refs) - allowed:
                raise PipelineError("Wiki Builder 返回了不存在的 Evidence 引用")
            kind = str(page.get("kind") or "analysis")
            relative = f"wiki/{self._page_directory(kind)}/{slugify(title)}.md"
            target = self.vault / relative
            previous = state.get("pages", {}).get(relative, {})
            source_ids = list(
                dict.fromkeys([*previous.get("source_ids", []), package.package_id])
            )
            versions = list(
                dict.fromkeys([*previous.get("source_versions", []), package.checksum[:12]])
            )
            evidence_ids = list(dict.fromkeys([*previous.get("evidence_ids", []), *refs]))
            evidence_block, evidence_modalities, visual_evidence_ids = (
                self._render_page_evidence(evidence_ids, evidence_records)
            )
            summary = str(page.get("summary") or "").strip()
            previous_layers = [
                str(value) for value in previous.get("representation_layers", [])
            ]
            representation_layers = list(
                dict.fromkeys(
                    [
                        *previous_layers,
                        "text",
                        *(
                            ("multimodal",)
                            if stage == "multimodal"
                            or set(evidence_modalities) & RICH_EVIDENCE_ITEM_TYPES
                            else ()
                        ),
                    ]
                )
            )
            revision = int(previous.get("revision") or 0) + 1
            updated_at = utc_now()
            frontmatter = (
                "---\n"
                f"title: {yaml_string(title)}\n"
                f"kind: {yaml_string(kind)}\n"
                f"summary: {yaml_string(summary)}\n"
                f"source_ids: {json.dumps(source_ids, ensure_ascii=False)}\n"
                f"source_versions: {json.dumps(versions, ensure_ascii=False)}\n"
                f"evidence_ids: {json.dumps(evidence_ids, ensure_ascii=False)}\n"
                f"evidence_modalities: {json.dumps(evidence_modalities, ensure_ascii=False)}\n"
                f"visual_evidence_ids: {json.dumps(visual_evidence_ids, ensure_ascii=False)}\n"
                f"representation_layers: {json.dumps(representation_layers, ensure_ascii=False)}\n"
                f"last_ingest_stage: {yaml_string(stage)}\n"
                f"revision: {revision}\n"
                f"builder_model: {yaml_string(str(plan.get('_model') or 'unknown'))}\n"
                f"analysis_model: {yaml_string(str(plan.get('_analysis_model') or plan.get('_model') or 'unknown'))}\n"
                f"prompt_version: {yaml_string(WIKI_PROMPT_VERSION)}\n"
                f"lifecycle: {yaml_string(str(previous.get('lifecycle') or 'draft'))}\n"
                f"updated_at: {yaml_string(updated_at)}\n"
                "---\n"
            )
            _atomic_write_text(
                target,
                f"{frontmatter}\n# {title}\n\n{content}\n\n{evidence_block}",
            )
            output.append(
                {
                    "title": title,
                    "kind": kind,
                    "summary": summary,
                    "path": relative,
                    "source_ids": source_ids,
                    "source_versions": versions,
                    "evidence_ids": evidence_ids,
                    "evidence_modalities": evidence_modalities,
                    "visual_evidence_ids": visual_evidence_ids,
                    "representation_layers": representation_layers,
                    "last_ingest_stage": stage,
                    "revision": revision,
                    "lifecycle": str(previous.get("lifecycle") or "draft"),
                    "updated_at": updated_at,
                }
            )
        return output

    def refresh_wiki_pages(self) -> dict[str, Any]:
        """Locally refresh deterministic Wiki wrappers without calling a model."""
        state = self._load_state()
        evidence_records = self._evidence_records(state)
        refreshed: list[str] = []
        skipped: list[str] = []
        for relative, page in state.get("pages", {}).items():
            target = (self.vault / relative).resolve()
            if self.vault.resolve() not in target.parents or not target.is_file():
                skipped.append(str(relative))
                continue
            title = str(page.get("title") or target.stem)
            kind = str(page.get("kind") or "analysis")
            content = _clean_generated_content(
                target.read_text(encoding="utf-8"), title
            )
            source_ids = list(dict.fromkeys(map(str, page.get("source_ids", []))))
            source_versions = list(
                dict.fromkeys(map(str, page.get("source_versions", [])))
            )
            evidence_ids = list(
                dict.fromkeys(map(str, page.get("evidence_ids", [])))
            )
            evidence_block, evidence_modalities, visual_evidence_ids = (
                self._render_page_evidence(evidence_ids, evidence_records)
            )
            source_records = [
                state.get("sources", {}).get(source_id, {})
                for source_id in source_ids
            ]
            builder_models = list(
                dict.fromkeys(
                    str(source.get("model") or "unknown")
                    for source in source_records
                )
            )
            analysis_models = list(
                dict.fromkeys(
                    str(source.get("analysis_model") or source.get("model") or "unknown")
                    for source in source_records
                )
            )
            summary = str(page.get("summary") or "").strip()
            updated_at = str(page.get("updated_at") or utc_now())
            representation_layers = [
                str(value) for value in page.get("representation_layers", [])
            ]
            if not representation_layers:
                representation_layers = ["text"]
                if set(evidence_modalities) & RICH_EVIDENCE_ITEM_TYPES:
                    representation_layers.append("multimodal")
            last_ingest_stage = str(
                page.get("last_ingest_stage")
                or ("multimodal" if "multimodal" in representation_layers else "text")
            )
            revision = max(1, int(page.get("revision") or 1))
            frontmatter = (
                "---\n"
                f"title: {yaml_string(title)}\n"
                f"kind: {yaml_string(kind)}\n"
                f"summary: {yaml_string(summary)}\n"
                f"source_ids: {json.dumps(source_ids, ensure_ascii=False)}\n"
                f"source_versions: {json.dumps(source_versions, ensure_ascii=False)}\n"
                f"evidence_ids: {json.dumps(evidence_ids, ensure_ascii=False)}\n"
                f"evidence_modalities: {json.dumps(evidence_modalities, ensure_ascii=False)}\n"
                f"visual_evidence_ids: {json.dumps(visual_evidence_ids, ensure_ascii=False)}\n"
                f"representation_layers: {json.dumps(representation_layers, ensure_ascii=False)}\n"
                f"last_ingest_stage: {yaml_string(last_ingest_stage)}\n"
                f"revision: {revision}\n"
                f"builder_model: {yaml_string(', '.join(builder_models))}\n"
                f"analysis_model: {yaml_string(', '.join(analysis_models))}\n"
                'prompt_version: "preserved-content+multimodal-evidence-v1"\n'
                f"lifecycle: {yaml_string(str(page.get('lifecycle') or 'draft'))}\n"
                f"updated_at: {yaml_string(updated_at)}\n"
                "---\n"
            )
            _atomic_write_text(
                target,
                f"{frontmatter}\n# {title}\n\n{content}\n\n{evidence_block}",
            )
            page.update(
                {
                    "summary": summary,
                    "evidence_modalities": evidence_modalities,
                    "visual_evidence_ids": visual_evidence_ids,
                    "representation_layers": representation_layers,
                    "last_ingest_stage": last_ingest_stage,
                    "revision": revision,
                    "lifecycle": str(page.get("lifecycle") or "draft"),
                    "updated_at": updated_at,
                }
            )
            refreshed.append(str(relative))
        self._save_state(state)
        self._write_navigation(state)
        self._log(
            "refresh-pages",
            f"{len(refreshed)} refreshed · {len(skipped)} skipped · local-only",
        )
        return {
            "status": "refreshed" if not skipped else "partial",
            "refreshed_pages": refreshed,
            "skipped_pages": skipped,
            "external_api_calls": 0,
        }

    def curate_sources(
        self,
        keep_source_ids: set[str],
        apply: bool = False,
    ) -> dict[str, Any]:
        """Restrict the active Vault/index corpus while preserving immutable Raw."""
        state = self._load_state()
        available = set(map(str, state.get("sources", {})))
        keep = set(map(str, keep_source_ids))
        if not keep:
            raise PipelineError("curate 至少需要一个 --keep 来源")
        unknown = sorted(keep - available)
        if unknown:
            raise PipelineError(f"curate 包含未知来源：{unknown}")
        excluded = sorted(available - keep)
        removed_pages: list[str] = []
        mixed_pages: list[str] = []
        for relative, page in state.get("pages", {}).items():
            page_sources = set(map(str, page.get("source_ids", [])))
            if page_sources and page_sources <= set(excluded):
                removed_pages.append(str(relative))
            elif page_sources & set(excluded):
                mixed_pages.append(str(relative))
        if mixed_pages:
            raise PipelineError(
                "存在同时引用保留与排除来源的稳定页，需先人工拆分："
                + ", ".join(sorted(mixed_pages))
            )

        vault_paths: set[str] = set(removed_pages)
        raw_paths: list[str] = []
        for source_id in excluded:
            source = state["sources"][source_id]
            for field in ("wiki_path", "evidence_map_path"):
                if source.get(field):
                    vault_paths.add(str(source[field]))
            vault_paths.add(f"assets/{slugify(source_id)}")
            raw_paths.append(
                f"runtime/raw/{slugify(source_id)}/{source.get('source_version', '')}"
            )
        result = {
            "status": "curated" if apply else "dry_run",
            "kept_source_ids": sorted(keep),
            "excluded_source_ids": excluded,
            "removed_vault_paths": sorted(vault_paths),
            "removed_stable_pages": sorted(removed_pages),
            "preserved_raw_paths": raw_paths,
            "preserved_query_history": True,
            "recoverable_by_reingest": True,
        }
        if not apply or not excluded:
            return result

        vault_root = self.vault.resolve()
        for relative in sorted(vault_paths):
            raw = Path(relative)
            if raw.is_absolute() or ".." in raw.parts:
                raise PipelineError(f"拒绝删除不安全的 Vault 路径：{relative}")
            target = (vault_root / raw).resolve()
            if vault_root not in target.parents:
                raise PipelineError(f"拒绝删除 Vault 之外的路径：{relative}")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()

        state["sources"] = {
            source_id: source
            for source_id, source in state.get("sources", {}).items()
            if source_id in keep
        }
        state["pages"] = {
            relative: page
            for relative, page in state.get("pages", {}).items()
            if relative not in removed_pages
        }
        self._save_state(state)

        if self.retrieval_index_path.is_file():
            index = json.loads(self.retrieval_index_path.read_text(encoding="utf-8"))
            if isinstance(index.get("sources"), dict):
                index["sources"] = {
                    source_id: value
                    for source_id, value in index["sources"].items()
                    if source_id in keep
                }
            for channel in ("text", "visual"):
                records = index.get(channel, {}).get("records", [])
                if isinstance(records, list):
                    index[channel]["records"] = [
                        record
                        for record in records
                        if str(record.get("source_id") or "") in keep
                    ]
            index["curated_at"] = utc_now()
            index["active_source_ids"] = sorted(keep)
            _atomic_write_text(
                self.retrieval_index_path,
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            )

        record = {**result, "created_at": utc_now()}
        with self.curation_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._write_navigation(state)
        self._log(
            "curate",
            f"keep {len(keep)} · exclude {len(excluded)} · Raw preserved",
        )
        return result

    def apply_reviewed_wiki_plan(
        self,
        package_path: str | Path,
        plan_path: str | Path,
    ) -> dict[str, Any]:
        """Apply a reviewed local Wiki plan without sending source data externally."""
        package = load_package(package_path)
        state = self._load_state()
        source = state.get("sources", {}).get(package.package_id)
        if source is None:
            raise PipelineError("本地知识编辑只能用于当前活跃语料中的来源")
        if str(source.get("checksum") or "") != package.checksum:
            raise PipelineError("知识编辑计划的 Source Package 版本与当前状态不一致")
        target = Path(plan_path).expanduser().resolve()
        if not target.is_file():
            raise PipelineError(f"知识编辑计划不存在：{target}")
        plan = json.loads(target.read_text(encoding="utf-8"))
        allowed = {
            self._item_evidence_id(package, item.item_id) for item in package.items
        }
        plan = validate_wiki_compilation(plan, allowed)
        plan["_model"] = "human-reviewed-local-plan"
        plan["_analysis_model"] = "human-reviewed-local-plan"
        generated_pages = self._write_generated_pages(
            package,
            plan,
            str(source.get("wiki_path") or ""),
            state,
            source.get("assets", {}),
        )
        generated_paths = list(
            dict.fromkeys(
                [
                    *source.get("generated_paths", []),
                    *(page["path"] for page in generated_pages),
                ]
            )
        )
        source["generated_paths"] = generated_paths
        source["knowledge_editor"] = "human-reviewed-local-plan"
        source["knowledge_edited_at"] = utc_now()
        for page in generated_pages:
            state["pages"][page["path"]] = page
        self._save_state(state)
        self._write_navigation(state)
        self._log(
            "knowledge-edit",
            f"{package.package_id} · {len(generated_pages)} pages · local reviewed plan",
        )
        return {
            "status": "edited",
            "package_id": package.package_id,
            "generated_pages": [page["path"] for page in generated_pages],
            "external_api_calls": 0,
        }

    def _wiki_catalog(
        self,
        state: dict[str, Any],
        focus_source_id: str | None = None,
        stage: str = "text",
    ) -> list[dict[str, Any]]:
        return [
            {
                "title": page["title"],
                "summary": page.get("summary", ""),
                "path": page["path"],
                "kind": page.get("kind", "analysis"),
                "evidence_modalities": page.get("evidence_modalities", []),
                "source_count": len(page.get("source_ids", [])),
                "update_eligible": bool(
                    stage != "multimodal"
                    or focus_source_id in set(map(str, page.get("source_ids", [])))
                ),
                "representation_layers": page.get("representation_layers", ["text"]),
                "revision": int(page.get("revision") or 1),
            }
            for page in state.get("pages", {}).values()
        ]

    def _existing_pages_for_actions(
        self, analysis: dict[str, Any], state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        requested = {
            (
                str(action.get("title") or "").strip().casefold(),
                str(action.get("kind") or "").strip(),
            )
            for action in analysis.get("page_actions", [])
            if isinstance(action, dict)
        }
        result: list[dict[str, Any]] = []
        for page in state.get("pages", {}).values():
            identity = (
                str(page["title"]).strip().casefold(),
                str(page.get("kind") or "").strip(),
            )
            if identity not in requested:
                continue
            path = self.vault / page["path"]
            raw_content = path.read_text(encoding="utf-8") if path.is_file() else ""
            result.append(
                {
                    "title": page["title"],
                    "path": page["path"],
                    "content": _clean_generated_content(
                        raw_content, str(page["title"])
                    )[:20000],
                    "evidence_ids": list(
                        dict.fromkeys(
                            str(value) for value in page.get("evidence_ids", [])
                        )
                    ),
                }
            )
        return result

    def _preserved_evidence_ids(
        self,
        package: Package,
        existing_pages: list[dict[str, Any]],
    ) -> set[str]:
        package_evidence_ids = {
            self._item_evidence_id(package, item.item_id)
            for item in package.items
        }
        return {
            str(evidence_id)
            for page in existing_pages
            if isinstance(page, dict)
            for evidence_id in page.get("evidence_ids", [])
            if str(evidence_id) in package_evidence_ids
        }

    def ingest_existing_wiki(
        self,
        wiki_root: str | Path,
        caption_package: str | Path | None = None,
    ) -> dict[str, Any]:
        """Create a derived, searchable view of a user's local Markdown Wiki."""

        source_root = Path(wiki_root).expanduser().resolve()
        if not source_root.is_dir():
            raise PipelineError(f"Wiki 目录不存在：{source_root}")
        wiki_id = slugify(source_root.name)
        output_root = self.wiki_root / "external" / wiki_id
        old_manifest_path = output_root / "manifest.json"
        old_manifest: dict[str, Any] = {}
        if old_manifest_path.is_file():
            try:
                old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old_manifest = {}
        caption_map = load_caption_map(Path(caption_package)) if caption_package else {}
        manifest = materialize_wiki(source_root, output_root, caption_map)
        source_id = f"external-{wiki_id}"
        state = self._load_state()
        state.setdefault("sources", {})
        state.setdefault("pages", {})
        for path, page in list(state["pages"].items()):
            if source_id in {str(value) for value in page.get("source_ids", [])}:
                del state["pages"][path]

        manifest_assets = manifest.get("assets", [])
        assets: dict[str, dict[str, Any]] = {}
        asset_by_name: dict[str, str] = {}
        for record in manifest_assets:
            derived = Path(str(record.get("derived_path") or ""))
            try:
                vault_path = derived.relative_to(self.vault).as_posix()
            except ValueError:
                continue
            asset_id = str(record.get("asset_id") or "")
            if not asset_id:
                continue
            assets[asset_id] = {
                "asset_id": asset_id,
                "vault_path": vault_path,
                "media_type": record.get("media_type") or "application/octet-stream",
                "sha256": record.get("sha256") or "",
                "source_path": record.get("source_path") or "",
                "caption": record.get("caption") or "",
                "caption_provenance": record.get("caption_provenance") or "missing",
            }
            asset_by_name[derived.name] = asset_id

        items: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        page_count = 0
        page_paths: list[str] = []
        for page_record in manifest.get("pages", []):
            derived_path = Path(str(page_record.get("derived_path") or ""))
            if not derived_path.is_file():
                continue
            try:
                relative = derived_path.relative_to(output_root)
            except ValueError:
                continue
            if relative.parts[:1] != ("pages",):
                continue
            page_relative = relative.relative_to("pages")
            vault_page = (output_root.relative_to(self.vault) / "pages" / page_relative).as_posix()
            content = derived_path.read_text(encoding="utf-8")
            item_id = "page-" + hashlib.sha256(str(page_relative).encode("utf-8")).hexdigest()[:20]
            used_asset_ids = [
                asset_id
                for filename, asset_id in asset_by_name.items()
                if f"assets/{filename}" in content
            ]
            item = {
                "item_id": item_id,
                "sequence": page_count,
                "item_type": "markdown_page",
                "page_start": None,
                "page_end": None,
                "bbox": {},
                "breadcrumb": str(page_relative),
                "raw_text": content,
                "caption": "\n".join(
                    str(assets[asset_id].get("caption") or "")
                    for asset_id in used_asset_ids
                    if assets[asset_id].get("caption")
                ),
                "search_text": content,
                "table": None,
                "equation": None,
                "semantic": {},
                "asset_ids": used_asset_ids,
                "provenance": {
                    "source_path": str(source_root / page_relative),
                    "derived_path": str(derived_path),
                    "source_type": "external_markdown",
                },
                "quality": {"caption_source": "mineru" if used_asset_ids else "none"},
                "searchable": True,
                "metadata": {"derived_page": vault_page},
            }
            evidence_id = f"{source_id}@{manifest['source_version']}#{item_id}"
            items.append(item)
            chunks.append(
                {
                    "chunk_id": "chunk-" + item_id,
                    "item_ids": [item_id],
                    "text": content,
                    "breadcrumb": str(page_relative),
                    "modalities": ["text"] + (["image"] if used_asset_ids else []),
                    "asset_ids": used_asset_ids,
                    "page_refs": [],
                    "provenance": {"evidence_id": evidence_id, "path": vault_page},
                    "quality": {},
                }
            )
            state["pages"][vault_page] = {
                "path": vault_page,
                "title": page_relative.stem,
                "kind": "external_markdown",
                "summary": " ".join(content.split())[:500],
                "source_ids": [source_id],
                "source_versions": [manifest["source_version"]],
                "evidence_ids": [evidence_id],
                "evidence_modalities": ["text"] + (["image"] if used_asset_ids else []),
                "representation_layers": ["text"] + (["image"] if used_asset_ids else []),
                "revision": 1,
            }
            page_paths.append(vault_page)
            page_count += 1

        if not page_paths:
            raise PipelineError("Wiki 中没有可导入的 Markdown 页面")
        source = {
            "title": source_root.name,
            "source_version": manifest["source_version"],
            "checksum": manifest["source_version"],
            "source_filename": source_root.name,
            "source_media_type": "text/markdown",
            "parser_name": "external-markdown-overlay",
            "wiki_path": page_paths[0],
            "evidence_map_path": "",
            "source_type": "external_markdown",
            "items": items,
            "chunks": chunks,
            "assets": assets,
            "stages": {"text": {"status": "completed", "provider": "local-overlay"}},
            "manifest_path": (output_root / "manifest.json").relative_to(self.vault).as_posix(),
        }
        state["sources"][source_id] = source
        self._save_state(state)
        self._write_navigation(state)
        self._log(
            "external-wiki-import",
            f"wiki={source_id} · pages={page_count} · assets={len(assets)} · errors={len(manifest.get('errors', []))}",
        )
        return {
            "status": "unchanged" if old_manifest.get("source_version") == manifest["source_version"] else "ingested",
            "source_id": source_id,
            "wiki_id": wiki_id,
            "source_version": manifest["source_version"],
            "derived_root": str(output_root),
            "pages": page_count,
            "assets": len(assets),
            "assets_copied": manifest.get("assets_copied", 0),
            "errors": manifest.get("errors", []),
            "caption_source": "mineru" if caption_map else "none",
        }

    def ingest(
        self,
        package_path: str | Path,
        provider: str = "baseline",
        force: bool = False,
        full_scale: bool = False,
        stage: str = "all",
    ) -> dict[str, Any]:
        if provider not in {"baseline", "api"}:
            raise PipelineError("provider 必须是 baseline 或 api")
        if stage not in INGEST_STAGES:
            raise PipelineError(f"stage 必须是 {', '.join(INGEST_STAGES)}")
        # Ingest remains usable when VLM is disabled: textual OCR/Caption
        # proxies continue through the existing pipeline, but no image payload
        # is sent to the vision model and full-scale visual analysis is skipped.
        if not self.features.enable_vlm:
            full_scale = False
        if full_scale and (provider != "api" or stage == "text"):
            raise PipelineError("full_scale 只支持 api provider 的多模态阶段")

        if stage == "all":
            started = time.perf_counter()
            text_result = self.ingest(
                package_path,
                provider=provider,
                force=force,
                full_scale=False,
                stage="text",
            )
            multimodal_result = self.ingest(
                package_path,
                provider=provider,
                force=force,
                full_scale=full_scale,
                stage="multimodal",
            )
            stage_results = {
                "text": text_result,
                "multimodal": multimodal_result,
            }
            metrics = [
                value.get("build_metrics", {}) for value in stage_results.values()
            ]
            statuses = {str(value.get("status") or "") for value in stage_results.values()}
            return {
                "status": (
                    "unchanged"
                    if statuses <= {"unchanged", "not_applicable"}
                    else "ingested"
                ),
                "package_id": str(
                    text_result.get("package_id")
                    or multimodal_result.get("package_id")
                    or ""
                ),
                "stage": "all",
                "stages": stage_results,
                "build_metrics": {
                    "elapsed_ms": round(
                        (time.perf_counter() - started) * 1000, 3
                    ),
                    "api_calls": sum(
                        int(value.get("api_calls") or 0) for value in metrics
                    ),
                    "token_usage": _merge_usage(
                        *[
                            value.get("token_usage", {})
                            for value in metrics
                            if isinstance(value, dict)
                        ]
                    ),
                    "created_pages": sum(
                        int(value.get("created_pages") or 0) for value in metrics
                    ),
                    "updated_pages": sum(
                        int(value.get("updated_pages") or 0) for value in metrics
                    ),
                    "multimodal_items_added": sum(
                        int(value.get("multimodal_items_added") or 0)
                        for value in metrics
                    ),
                },
            }

        started = time.perf_counter()
        load_started = time.perf_counter()
        package = load_package(package_path)
        timings: dict[str, float] = {
            "load_package_ms": round(
                (time.perf_counter() - load_started) * 1000, 3
            )
        }
        state = self._load_state()
        state.setdefault("pages", {})
        current = state["sources"].get(package.package_id)
        same_version = bool(current and current.get("checksum") == package.checksum)
        stages = dict(current.get("stages", {})) if same_version else {}
        previous_stage = stages.get(stage, {})
        if not force and previous_stage:
            same_configuration = (
                previous_stage.get("status") in {"completed", "not_applicable"}
                and previous_stage.get("provider") == provider
                and bool(previous_stage.get("full_scale")) == bool(full_scale)
            )
            if same_configuration:
                return {
                    "status": "unchanged",
                    "package_id": package.package_id,
                    "source_version": package.checksum[:12],
                    "stage": stage,
                    "build_metrics": {
                        "elapsed_ms": round(
                            (time.perf_counter() - started) * 1000, 3
                        ),
                        "api_calls": 0,
                        "token_usage": {},
                        "created_pages": 0,
                        "updated_pages": 0,
                        "multimodal_items_added": 0,
                        "idempotent_reuse": True,
                    },
                }

        # A fair text LLM Wiki baseline keeps textual proxies (OCR, captions and
        # linearized chunk text) for every item, but withholds structured tables,
        # equations, assets and visual semantics until the multimodal stage.
        text_item_ids = {item.item_id for item in package.items}
        multimodal_item_ids = {
            item.item_id for item in package.items if _is_multimodal_item(item)
        }
        if stage == "multimodal":
            legacy_text_ready = bool(
                same_version and current and current.get("wiki_path")
            )
            text_ready = bool(
                stages.get("text", {}).get("status") == "completed"
                or legacy_text_ready
            )
            if not text_ready:
                raise PipelineError(
                    "多模态增强必须建立在相同版本的文本 LLM Wiki 基座之上；"
                    "请先执行 --stage text"
                )
            if not multimodal_item_ids:
                stages["multimodal"] = {
                    "status": "not_applicable",
                    "provider": provider,
                    "full_scale": bool(full_scale),
                    "completed_at": utc_now(),
                    "evidence_items": 0,
                }
                current["stages"] = stages
                self._save_state(state)
                return {
                    "status": "not_applicable",
                    "package_id": package.package_id,
                    "source_version": package.checksum[:12],
                    "stage": stage,
                    "reason": "该来源没有表格、图片、图表或公式证据",
                    "build_metrics": {
                        "elapsed_ms": round(
                            (time.perf_counter() - started) * 1000, 3
                        ),
                        "api_calls": 0,
                        "token_usage": {},
                        "created_pages": 0,
                        "updated_pages": 0,
                        "multimodal_items_added": 0,
                    },
                }

        stage_item_ids = text_item_ids if stage == "text" else multimodal_item_ids
        active_item_ids = text_item_ids if stage == "text" else {
            item.item_id for item in package.items
        }
        active_item_objects = [
            item for item in package.items if item.item_id in active_item_ids
        ]
        active_chunk_objects = [
            chunk
            for chunk in package.chunks
            if active_item_ids.intersection(chunk.item_ids)
        ]
        active_items = (
            [_text_projection_item(item) for item in active_item_objects]
            if stage == "text"
            else [item.to_dict() for item in active_item_objects]
        )
        active_chunks = (
            [_text_projection_chunk(chunk) for chunk in active_chunk_objects]
            if stage == "text"
            else [chunk.to_dict() for chunk in active_chunk_objects]
        )
        stage_asset_ids = {
            asset_id
            for item in package.items
            if item.item_id in stage_item_ids
            for asset_id in item.asset_ids
        }

        raw_started = time.perf_counter()
        raw_target = self.raw_root / slugify(package.package_id) / package.checksum[:12]
        if not raw_target.exists():
            shutil.copytree(package.package_path, raw_target)
        timings["raw_copy_ms"] = round(
            (time.perf_counter() - raw_started) * 1000, 3
        )

        asset_started = time.perf_counter()
        assets = (
            self._copy_assets(package, stage_asset_ids)
            if stage == "multimodal"
            else {}
        )
        timings["asset_copy_ms"] = round(
            (time.perf_counter() - asset_started) * 1000, 3
        )

        if stage == "multimodal" and current:
            assets = {**current.get("assets", {}), **assets}
        source_target = self.wiki_root / "sources" / f"{slugify(package.package_id)}.md"
        source_started = time.perf_counter()
        _atomic_write_text(
            source_target,
            self._baseline_page(
                package,
                assets,
                active_item_ids,
                text_only=stage == "text",
            ),
        )
        timings["source_page_ms"] = round(
            (time.perf_counter() - source_started) * 1000, 3
        )
        generated_pages: list[dict[str, Any]] = []
        model = "deterministic-baseline"
        analysis_model = "deterministic-baseline"
        visual_analysis = {
            "candidate_images": 0,
            "analyzed_images": 0,
            "max_images": _bounded_env_int("MMWIKI_MAX_BUILD_IMAGES", 8, 1, 16),
            "truncated": False,
            "used_actual_images": False,
        }
        analysis_usage: dict[str, Any] | list[dict[str, Any]] = {}
        compile_usage: dict[str, Any] = {}
        api_calls = 0
        existing_page_paths = set(state.get("pages", {}))
        impact_candidates = sorted(
            str(page.get("path") or path)
            for path, page in state.get("pages", {}).items()
            if package.package_id in set(map(str, page.get("source_ids", [])))
        )
        if provider == "api":
            llm = OpenAICompatibleProvider(self.root, "build")
            vision_llm = OpenAICompatibleProvider(self.root, "vision")
            evidence = self._builder_evidence(
                package,
                stage_item_ids,
                text_only=stage == "text",
            )
            if not evidence:
                raise PipelineError(f"{stage} 阶段没有可供 Wiki 编译的 Evidence")
            schema = self._wiki_instructions()
            analysis_started = time.perf_counter()
            if full_scale:
                analysis, visual_analysis, analysis_model = (
                    self._analyze_wiki_full_scale(
                        package,
                        evidence,
                        assets,
                        state,
                        schema,
                        llm,
                        vision_llm,
                    )
                )
                analysis_usage = analysis.get("batch_usage", [])
                api_calls += sum(
                    not bool(batch.get("cached"))
                    for batch in visual_analysis.get("batches", [])
                )
            else:
                image_payloads, visual_analysis = self._builder_image_payloads(
                    package,
                    assets,
                    evidence_ids={str(value["id"]) for value in evidence},
                )
                analyzer = (
                    vision_llm
                    if stage == "multimodal"
                    and self.features.enable_vlm
                    and image_payloads
                    and vision_llm.configured
                    else llm
                )
                analysis_images = image_payloads if analyzer is vision_llm else []
                visual_analysis["used_actual_images"] = bool(analysis_images)
                analysis = analyzer.analyze_wiki(
                    package.title,
                    evidence,
                    self._wiki_catalog(state, package.package_id, stage=stage),
                    schema,
                    analysis_images,
                    stage=stage,
                )
                analysis_model = analyzer.model
                analysis_usage = analysis.get("_usage", {})
                api_calls += 1
            timings["analysis_ms"] = round(
                (time.perf_counter() - analysis_started) * 1000, 3
            )
            compile_started = time.perf_counter()
            existing_pages = self._existing_pages_for_actions(analysis, state)
            plan = llm.compile_wiki(
                package.title,
                analysis,
                evidence,
                existing_pages,
                schema,
                stage=stage,
                preserved_evidence_ids=self._preserved_evidence_ids(
                    package, existing_pages
                ),
            )
            compile_usage = plan.get("_usage", {})
            api_calls += 1
            plan["_model"] = llm.model
            plan["_analysis_model"] = analysis_model
            model = llm.model
            timings["compile_ms"] = round(
                (time.perf_counter() - compile_started) * 1000, 3
            )
            write_started = time.perf_counter()
            generated_pages = self._write_generated_pages(
                package,
                plan,
                source_target.relative_to(self.vault).as_posix(),
                state,
                assets,
                stage=stage,
            )
            timings["write_pages_ms"] = round(
                (time.perf_counter() - write_started) * 1000, 3
            )
        generated_paths = [page["path"] for page in generated_pages]
        created_pages = sum(path not in existing_page_paths for path in generated_paths)
        updated_pages = len(generated_paths) - created_pages
        previous_generated_paths = (
            list(current.get("generated_paths", []))
            if stage == "multimodal" and current
            else []
        )
        effective_generated_paths = list(
            dict.fromkeys([*previous_generated_paths, *generated_paths])
        )
        if stage == "text":
            stages.pop("multimodal", None)

        build_metrics = {
            "elapsed_ms": 0.0,
            "timings": timings,
            "api_calls": api_calls,
            "token_usage": _merge_usage(analysis_usage, compile_usage),
            "analysis_usage": analysis_usage,
            "compile_usage": compile_usage,
            "created_pages": created_pages,
            "updated_pages": updated_pages,
            "touched_pages": generated_paths,
            "impact_scope": {
                "candidate_pages": impact_candidates,
                "candidate_page_count": len(impact_candidates),
                "touched_pages": generated_paths,
                "touched_page_count": len(generated_paths),
                "preserved_page_count": len(existing_page_paths - set(generated_paths)),
                "strategy": (
                    "text-wiki-bootstrap"
                    if stage == "text"
                    else "source-linked-page-increment"
                ),
            },
            "multimodal_items_added": (
                len(multimodal_item_ids) if stage == "multimodal" else 0
            ),
            "active_items": len(active_items),
            "active_chunks": len(active_chunks),
            "active_assets": len(assets),
            "idempotent_reuse": False,
        }
        stages[stage] = {
            "status": "completed",
            "provider": provider,
            "model": model,
            "analysis_model": analysis_model,
            "full_scale": bool(full_scale),
            "completed_at": utc_now(),
            "evidence_items": len(stage_item_ids),
            "generated_paths": generated_paths,
            "build_metrics": build_metrics,
        }
        source_record = {
            "package_id": package.package_id,
            "title": package.title,
            "checksum": package.checksum,
            "source_version": package.checksum[:12],
            "provider": provider,
            "model": model,
            "analysis_model": analysis_model,
            "visual_analysis": visual_analysis,
            "full_scale": full_scale,
            "representation": "text+multimodal" if stage == "multimodal" else "text",
            "representation_layers": (
                ["text", "multimodal"] if stage == "multimodal" else ["text"]
            ),
            "stages": stages,
            "wiki_path": source_target.relative_to(self.vault).as_posix(),
            "evidence_map_path": (
                f"wiki/evidence/{slugify(package.package_id)}-multimodal.md"
            ),
            "generated_paths": effective_generated_paths,
            "items": active_items,
            "chunks": active_chunks,
            "assets": assets,
            "ingested_at": utc_now(),
        }
        state["sources"][package.package_id] = source_record
        for page in generated_pages:
            state["pages"][page["path"]] = page
        self._save_state(state)
        self._write_navigation(state)
        build_metrics["elapsed_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )
        build_metrics["vault_bytes_after"] = _directory_bytes(self.vault)
        stages[stage]["build_metrics"] = build_metrics
        source_record["stages"] = stages
        self._save_state(state)
        self._log(
            "ingest",
            f"{package.package_id} · {stage} · {provider} · {model}",
        )
        return {
            "status": "ingested",
            "package_id": package.package_id,
            "source_version": package.checksum[:12],
            "stage": stage,
            "provider": provider,
            "model": model,
            "analysis_model": analysis_model,
            "visual_analysis": visual_analysis,
            "full_scale": full_scale,
            "wiki_path": source_record["wiki_path"],
            "generated_pages": generated_paths,
            "build_metrics": build_metrics,
            "counts": {
                "items": len(active_items),
                "chunks": len(active_chunks),
                "assets": len(assets),
            },
        }

    @staticmethod
    def _markdown_cell(value: Any) -> str:
        return " ".join(str(value or "").split()).replace("|", "\\|")

    def _write_evidence_maps(self, state: dict[str, Any]) -> None:
        for source_id, source in state.get("sources", {}).items():
            evidence_map_path = str(
                source.get("evidence_map_path")
                or f"wiki/evidence/{slugify(str(source_id))}-multimodal.md"
            )
            target = self.vault / evidence_map_path
            version = str(source.get("source_version") or "")
            prefix = f"{source_id}@{version}#"
            items = [item for item in source.get("items", []) if isinstance(item, dict)]
            rich_items = [
                item
                for item in items
                if str(item.get("item_type") or "") in RICH_EVIDENCE_ITEM_TYPES
                or item.get("table")
                or item.get("equation")
                or item.get("asset_ids")
            ]
            outline: dict[str, dict[str, Any]] = {}
            for item in items:
                section = str(item.get("breadcrumb") or "未分节")
                record = outline.setdefault(
                    section, {"pages": set(), "modalities": set(), "count": 0}
                )
                record["count"] += 1
                if item.get("page_start") is not None:
                    record["pages"].add(int(item["page_start"]))
                record["modalities"].add(str(item.get("item_type") or "text"))
            outline_lines = ["| 章节 | 页码 | Items | 模态 |", "|---|---:|---:|---|"]
            for section, record in outline.items():
                pages = sorted(record["pages"])
                page_text = ", ".join(str(value) for value in pages) or "—"
                outline_lines.append(
                    "| "
                    + " | ".join(
                        [
                            self._markdown_cell(section),
                            page_text,
                            str(record["count"]),
                            ", ".join(sorted(record["modalities"])),
                        ]
                    )
                    + " |"
                )
            blocks: list[str] = []
            source_link = str(source.get("wiki_path") or "").removesuffix(".md")
            for item in rich_items:
                item_id = str(item.get("item_id") or "")
                evidence_id = prefix + item_id
                item_type = str(item.get("item_type") or "unknown")
                page = item.get("page_start")
                heading = " · ".join(
                    value
                    for value in (
                        item_type,
                        f"第 {page} 页" if page is not None else "",
                        str(item.get("breadcrumb") or ""),
                    )
                    if value
                )
                lines = [f"### {heading or item_id}", ""]
                caption = str(item.get("caption") or "").strip()
                if caption:
                    lines.extend([f"**原始 Caption：** {caption}", ""])
                description = str(
                    (item.get("semantic") or {}).get("description") or ""
                ).strip()
                if description and description != caption:
                    lines.extend(
                        [
                            "> [!note] 上游语义说明（派生信息，不替代原图）",
                            f"> {description}",
                            "",
                        ]
                    )
                table = _table_markdown(item.get("table"), 12000)
                if table:
                    lines.extend([table, ""])
                equation = item.get("equation") or {}
                if equation.get("latex"):
                    lines.extend([f"$${equation['latex']}$$", ""])
                for asset_id in item.get("asset_ids", []):
                    vault_path = str(
                        source.get("assets", {}).get(asset_id, {}).get("vault_path")
                        or ""
                    )
                    if vault_path:
                        lines.extend([f"![[{vault_path}]]", ""])
                lines.extend(
                    [
                        f"Evidence ID：`{evidence_id}`",
                        f"来源：[[{source_link}#{item_id}|打开来源记录]]",
                    ]
                )
                blocks.append("\n".join(lines))
            modalities = sorted(
                {str(item.get("item_type") or "text") for item in rich_items}
            )
            evidence_ids = [
                prefix + str(item.get("item_id") or "") for item in rich_items
            ]
            frontmatter = (
                "---\n"
                f"title: {yaml_string(str(source.get('title') or source_id) + ' · 多模态证据地图')}\n"
                "kind: \"evidence-map\"\n"
                f"source_ids: {json.dumps([str(source_id)], ensure_ascii=False)}\n"
                f"source_versions: {json.dumps([version], ensure_ascii=False)}\n"
                f"evidence_ids: {json.dumps(evidence_ids, ensure_ascii=False)}\n"
                f"evidence_modalities: {json.dumps(modalities, ensure_ascii=False)}\n"
                "---\n"
            )
            body = (
                f"{frontmatter}\n# {source.get('title') or source_id} · 多模态证据地图\n\n"
                "> 本页由 Pipeline 确定性生成。原始 Caption、上游语义说明和实际视觉资源分开展示；"
                "语义说明不能替代原图。\n\n"
                "## 文档结构\n\n"
                + "\n".join(outline_lines)
                + "\n\n## 图片、图表、表格与公式\n\n"
                + ("\n\n".join(blocks) or "该来源暂无结构化多模态 Evidence。")
                + "\n"
            )
            _atomic_write_text(target, body)

    def _wiki_graph(self, state: dict[str, Any]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        for source_id, source in state.get("sources", {}).items():
            nodes.append(
                {
                    "path": str(source.get("wiki_path") or ""),
                    "title": str(source.get("title") or source_id),
                    "kind": "source",
                }
            )
            nodes.append(
                {
                    "path": str(source.get("evidence_map_path") or ""),
                    "title": f"{source.get('title') or source_id} · 多模态证据地图",
                    "kind": "evidence-map",
                }
            )
        for page in state.get("pages", {}).values():
            nodes.append(
                {
                    "path": str(page.get("path") or ""),
                    "title": str(page.get("title") or ""),
                    "kind": str(page.get("kind") or "analysis"),
                }
            )
        nodes = [node for node in nodes if node["path"]]
        aliases: dict[str, str | None] = {}
        duplicate_titles: dict[str, list[str]] = {}
        for node in nodes:
            path = node["path"].removesuffix(".md")
            keys = {path, Path(path).name, node["title"]}
            for key in keys:
                normalized = key.casefold()
                if normalized in aliases and aliases[normalized] != node["path"]:
                    aliases[normalized] = None
                    duplicate_titles.setdefault(key, []).append(node["path"])
                else:
                    aliases[normalized] = node["path"]
        edges: set[tuple[str, str]] = set()
        wanted: dict[str, set[str]] = {}
        vault_root = self.vault.resolve()
        for node in nodes:
            target = (vault_root / node["path"]).resolve()
            if vault_root not in target.parents or not target.is_file():
                continue
            content = target.read_text(encoding="utf-8")
            for raw_link in WIKILINK_PATTERN.findall(content):
                link = raw_link.strip().removesuffix(".md")
                resolved = aliases.get(link.casefold())
                if resolved:
                    if resolved != node["path"]:
                        edges.add((node["path"], resolved))
                else:
                    wanted.setdefault(link, set()).add(node["path"])
        inbound = {node["path"]: 0 for node in nodes}
        for _, target in edges:
            inbound[target] = inbound.get(target, 0) + 1
        for node in nodes:
            node["inbound_links"] = inbound.get(node["path"], 0)
        stable_paths = {
            str(page.get("path") or "") for page in state.get("pages", {}).values()
        }
        orphans = sorted(path for path in stable_paths if path and inbound.get(path, 0) == 0)
        provenance_edges = [
            {
                "page": str(page.get("path") or path),
                "source_id": str(source_id),
                "evidence_ids": [
                    str(value) for value in page.get("evidence_ids", [])
                    if str(value).startswith(f"{source_id}@")
                ],
            }
            for path, page in state.get("pages", {}).items()
            for source_id in page.get("source_ids", [])
        ]
        return {
            "nodes": nodes,
            "edges": [
                {"source": source, "target": target}
                for source, target in sorted(edges)
            ],
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "stable_pages": len(stable_paths),
                "orphan_stable_pages": len(orphans),
                "wanted_pages": len(wanted),
                "provenance_edges": len(provenance_edges),
            },
            "provenance_edges": provenance_edges,
            "authority": dict(sorted(inbound.items())),
            "orphans": orphans,
            "wanted_pages": {
                key: sorted(value) for key, value in sorted(wanted.items())
            },
            "duplicate_titles": duplicate_titles,
        }

    @staticmethod
    def _stale_pages(state: dict[str, Any]) -> list[dict[str, Any]]:
        current_versions = {
            str(source_id): str(source.get("source_version") or "")
            for source_id, source in state.get("sources", {}).items()
        }
        stale: list[dict[str, Any]] = []
        for path, page in state.get("pages", {}).items():
            recorded = set(map(str, page.get("source_versions", [])))
            mismatches = [
                {
                    "source_id": str(source_id),
                    "current_version": current_versions.get(str(source_id), ""),
                }
                for source_id in page.get("source_ids", [])
                if current_versions.get(str(source_id), "") not in recorded
            ]
            if mismatches:
                stale.append(
                    {
                        "path": str(page.get("path") or path),
                        "title": str(page.get("title") or path),
                        "mismatches": mismatches,
                    }
                )
        return stale

    def _write_maintenance(
        self, state: dict[str, Any], graph: dict[str, Any]
    ) -> dict[str, Any]:
        coverage = self._wiki_coverage(state)
        stale_pages = self._stale_pages(state)
        review_pages = sorted(
            str(page.get("path") or path)
            for path, page in state.get("pages", {}).items()
            if str(page.get("lifecycle") or "draft") in {"draft", "review_needed"}
        )
        status = "healthy" if not (
            stale_pages
            or graph["orphans"]
            or graph["wanted_pages"]
            or graph["duplicate_titles"]
        ) else "attention_needed"
        stale_lines = [
            f"- [[{item['path'].removesuffix('.md')}|{item['title']}]] — "
            + ", ".join(
                f"{value['source_id']}→{value['current_version']}"
                for value in item["mismatches"]
            )
            for item in stale_pages
        ]
        review_lines = [f"- [[{path.removesuffix('.md')}]]" for path in review_pages]
        _atomic_write_text(
            self.maintenance_path,
            "# Wiki Maintenance\n\n"
            "> 本页由 Pipeline 确定性生成，用于维护文本 Wiki 主干；不会自动改写知识页。\n\n"
            f"- 状态：`{status}`\n"
            f"- 来源覆盖：{coverage['stable_page_source_coverage']}/{coverage['stable_page_source_total']}\n"
            f"- 版本过期页面：{len(stale_pages)}\n"
            f"- 孤立稳定页：{len(graph['orphans'])}\n"
            f"- 待创建/断链目标：{len(graph['wanted_pages'])}\n"
            f"- 待人工复核页面：{len(review_pages)}\n\n"
            "## 版本过期页面\n\n"
            + ("\n".join(stale_lines) or "无")
            + "\n\n## 待人工复核页面\n\n"
            + ("\n".join(review_lines) or "无")
            + "\n",
        )
        return {
            "status": status,
            "stale_pages": stale_pages,
            "review_pages": review_pages,
        }

    def _write_graph_health(self, graph: dict[str, Any]) -> None:
        _atomic_write_text(
            self.graph_path,
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        )
        stats = graph["stats"]
        orphan_lines = [f"- [[{path.removesuffix('.md')}]]" for path in graph["orphans"]]
        wanted_lines = [
            f"- `{title}` ← " + ", ".join(f"[[{path.removesuffix('.md')}]]" for path in paths)
            for title, paths in graph["wanted_pages"].items()
        ]
        content = (
            "# Wiki Graph Health\n\n"
            f"- 节点：{stats['nodes']}\n"
            f"- 有效链接：{stats['edges']}\n"
            f"- 稳定知识页孤立数：{stats['orphan_stable_pages']}\n"
            f"- 待创建/断链目标：{stats['wanted_pages']}\n\n"
            "## 孤立稳定知识页\n\n"
            + ("\n".join(orphan_lines) or "无")
            + "\n\n## 待创建或断链页面\n\n"
            + ("\n".join(wanted_lines) or "无")
            + "\n"
        )
        _atomic_write_text(self.graph_report_path, content)

    def _write_navigation(self, state: dict[str, Any]) -> None:
        self._write_evidence_maps(state)
        graph = self._wiki_graph(state)
        self._write_graph_health(graph)
        maintenance = self._write_maintenance(state, graph)
        source_lines = [
            f"- [[{source['wiki_path'].removesuffix('.md')}|{source['title']}]]"
            for source in state["sources"].values()
        ]
        evidence_map_lines = [
            f"- [[{str(source.get('evidence_map_path') or '').removesuffix('.md')}|{source['title']} · 多模态证据地图]]"
            for source in state["sources"].values()
            if source.get("evidence_map_path")
        ]
        generated = []
        for page in sorted(
            state.get("pages", {}).values(), key=lambda value: value["path"]
        ):
            summary = self._markdown_cell(page.get("summary") or "暂无摘要")
            modalities = ", ".join(page.get("evidence_modalities", []))
            suffix = f" — {summary}"
            if modalities:
                suffix += f" · `{modalities}`"
            generated.append(
                f"- [[{page['path'].removesuffix('.md')}|{page['title']}]]{suffix}"
            )
        coverage = self._wiki_coverage(state)
        home = (
            "# 多模态 LLM Wiki\n\n"
            "本 Vault 由文档解析组交付的多模态 package 构建。点击左侧 `scan-search` 图标打开在线问答面板，"
            "系统先用来源页和稳定知识页提供 Wiki 导航信号，再检索上游 chunk，最后回读文字、完整表格和原始图片证据，由视觉模型用中文回答。\n\n"
            f"> 当前来源页覆盖 {coverage['source_pages']}/{coverage['source_pages']} 份来源；"
            f"稳定知识页覆盖 {coverage['stable_page_source_coverage']}/"
            f"{coverage['stable_page_source_total']} 份来源。\n\n"
            "## 浏览入口\n\n"
            "- [[wiki/index|Wiki Index]]\n"
            "- [[wiki/overview|Wiki Overview]]\n"
            "- [[wiki/graph-health|Wiki Graph Health]]\n"
            "- [[wiki/maintenance|Wiki Maintenance]]\n"
            "- [[wiki-purpose|Wiki Purpose]]\n\n"
            "## 来源数据\n\n"
            + ("\n".join(source_lines) or "暂无来源")
            + "\n\n## 多模态证据地图\n\n"
            + ("\n".join(evidence_map_lines) or "暂无来源")
            + "\n\n## 稳定知识页\n\n"
            + ("\n".join(generated) or "使用 `--provider api` 构建后显示。")
            + "\n"
        )
        _atomic_write_text(self.vault / "Home.md", home)
        index = (
            "# Wiki Index\n\n"
            "## Sources\n\n"
            + ("\n".join(source_lines) or "暂无来源")
            + "\n\n## Multimodal Evidence Maps\n\n"
            + ("\n".join(evidence_map_lines) or "暂无来源")
            + "\n\n## Pages\n\n"
            + ("\n".join(generated) or "暂无生成页")
            + "\n"
        )
        _atomic_write_text(self.index_path, index)
        by_kind: dict[str, list[str]] = {}
        for page in state.get("pages", {}).values():
            summary = self._markdown_cell(page.get("summary") or "暂无摘要")
            by_kind.setdefault(str(page.get("kind") or "analysis"), []).append(
                f"- [[{page['path'].removesuffix('.md')}|{page['title']}]] — {summary}"
            )
        sections = []
        labels = {"concept": "概念", "entity": "实体", "analysis": "分析"}
        for kind in ("concept", "entity", "analysis"):
            sections.append(
                f"## {labels[kind]}\n\n" + ("\n".join(by_kind.get(kind, [])) or "暂无")
            )
        _atomic_write_text(
            self.overview_path,
            "# Wiki Overview\n\n"
            f"当前收录 {len(state.get('sources', {}))} 个来源、"
            f"{len(state.get('pages', {}))} 个稳定知识页；稳定知识层覆盖 "
            f"{coverage['stable_page_source_coverage']}/"
            f"{coverage['stable_page_source_total']} 个来源。\n\n"
            f"图谱当前有 {graph['stats']['edges']} 条有效 WikiLink、"
            f"{graph['stats']['orphan_stable_pages']} 个孤立稳定页和 "
            f"{graph['stats']['wanted_pages']} 个待创建/断链目标。\n\n"
            f"维护状态为 `{maintenance['status']}`，版本过期页面 "
            f"{len(maintenance['stale_pages'])} 个。\n\n"
            + "\n\n".join(sections)
            + "\n",
        )

    def sources(self) -> list[dict[str, Any]]:
        state = self._load_state()
        page_counts: dict[str, int] = {}
        for page in state.get("pages", {}).values():
            for source_id in page.get("source_ids", []):
                page_counts[str(source_id)] = page_counts.get(str(source_id), 0) + 1
        return [
            {
                "source_id": package_id,
                "title": source["title"],
                "source_version": source["source_version"],
                "path": source["wiki_path"],
                "evidence_map_path": source.get("evidence_map_path"),
                "evidence_count": len(source.get("items", [])),
                "modalities": sorted({item["item_type"] for item in source.get("items", [])}),
                "visual_evidence_count": sum(
                    1
                    for item in source.get("items", [])
                    if item.get("item_type") in RICH_EVIDENCE_ITEM_TYPES
                    or item.get("asset_ids")
                    or item.get("table")
                    or item.get("equation")
                ),
                "stable_page_count": page_counts.get(str(package_id), 0),
            }
            for package_id, source in sorted(state["sources"].items())
        ]

    def retrieval_status(self) -> dict[str, Any]:
        state = self._load_state()
        provider = BailianRetrievalProvider(self.root)
        status = RetrievalIndex(
            self.retrieval_index_path, self.vault
        ).status(state, provider)
        return {
            **status,
            "text_configured": provider.text_configured,
            "multimodal_configured": provider.multimodal_configured,
            "feature_config": self.features.as_dict(),
            "vector_retrieval_enabled": self.features.enable_vector_retrieval,
            "vlm_enabled": self.features.enable_vlm,
            "available_modes": ["auto", *RETRIEVAL_MODES],
            "wiki_navigation_ready": bool(state.get("sources")),
            "wiki_page_navigation": {
                "lexical": bool(state.get("sources")),
                "semantic": bool(status.get("wiki_semantic_ready")),
                "strategy": "Wiki page -> source/chunk Evidence -> answer",
            },
            "wiki_coverage": self._wiki_coverage(state),
            "configured_remote_content_processing": bool(
                provider.text_configured or provider.multimodal_configured
            ),
            "remote_content_processing": bool(
                (self.features.enable_vlm and provider.multimodal_configured)
                or (self.features.enable_vector_retrieval and provider.text_configured)
            ),
        }

    def build_retrieval_index(
        self,
        include_visual: bool = True,
        source_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        if not self.features.enable_vector_retrieval:
            return {
                "status": "disabled",
                "reason": "向量检索已关闭；保留已有索引且不调用 Embedding/Rerank",
                "feature_config": self.features.as_dict(),
            }
        state = self._load_state()
        provider = BailianRetrievalProvider(self.root)
        result = RetrievalIndex(
            self.retrieval_index_path, self.vault
        ).build(
            state,
            provider,
            include_visual and self.features.enable_vlm,
            source_ids,
        )
        self._log(
            "retrieval-index",
            (
                f"wiki={result['wiki_records']} · text={result['text_records']} · "
                f"visual={result['visual_records']} · "
                f"{result['text_model']} · {result['visual_model']}"
            ),
        )
        return result

    def build_wiki_page_index(self) -> dict[str, Any]:
        """Build only Wiki-page vectors without touching existing Evidence vectors."""
        if not self.features.enable_vector_retrieval:
            return {
                "status": "disabled",
                "reason": "向量检索已关闭；不构建 Wiki 页面向量索引",
                "feature_config": self.features.as_dict(),
            }
        state = self._load_state()
        provider = BailianRetrievalProvider(self.root)
        result = RetrievalIndex(
            self.retrieval_index_path, self.vault
        ).build_wiki_pages(state, provider)
        self._log(
            "wiki-page-index",
            (
                f"wiki={result['wiki_records']} · new={result['new_wiki_records']} · "
                f"reused={result['reused_wiki_records']} · evidence_vectors=preserved"
            ),
        )
        return result

    def migrate_retrieval_index(self) -> dict[str, Any]:
        state = self._load_state()
        provider = BailianRetrievalProvider(self.root)
        result = RetrievalIndex(
            self.retrieval_index_path, self.vault
        ).migrate_legacy(state, provider)
        self._log(
            "retrieval-index-migration",
            (
                f"status={result['status']} · text={result['text_records']} · "
                f"visual={result['visual_records']} · external_api_calls=0"
            ),
        )
        return result

    def search_with_trace(
        self,
        question: str,
        top_k: int = 5,
        source_ids: set[str] | None = None,
        retrieval_mode: str = "auto",
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            return {
                "hits": [],
                "retrieval": {
                    "requested_mode": retrieval_mode,
                    "mode": "lexical",
                    "channels": [],
                    "models": {},
                    "fallback_reason": None,
                    "usage": {},
                },
            }
        if len(question) > 4000:
            raise PipelineError("问题长度不能超过 4000 个字符")
        if not 1 <= top_k <= 20:
            raise PipelineError("top_k 必须在 1 到 20 之间")
        state = self._load_state()
        visual_intent = any(
            word in question.casefold()
            for word in ("图", "图片", "图表", "figure", "chart", "image")
        )
        effective_mode = resolve_query_mode(
            retrieval_mode,
            visual_intent=visual_intent,
            config=self.features,
        )
        fallback_reason = None
        if effective_mode != retrieval_mode:
            if not self.features.enable_vector_retrieval:
                fallback_reason = "向量检索已关闭，已回退到 BM25 + MinerU Caption"
            elif retrieval_mode == "multimodal" and not self.features.enable_vlm:
                fallback_reason = "VLM 已关闭，已回退到文本混合检索"
        unknown_sources = set(source_ids or set()) - set(state.get("sources", {}))
        if unknown_sources:
            raise PipelineError(f"不存在的 source_id：{sorted(unknown_sources)}")
        wiki_navigation = navigate_wiki(
            state, self.vault, question, source_ids, limit=max(top_k, 8)
        )
        wiki_source_ranks: dict[str, int] = {}
        for rank, page in enumerate(wiki_navigation, 1):
            for source_id in page["source_ids"]:
                wiki_source_ranks.setdefault(str(source_id), rank)
        wiki_paths_by_source: dict[str, list[str]] = {}
        for source_id, source in state.get("sources", {}).items():
            evidence_map_path = str(source.get("evidence_map_path") or "")
            if evidence_map_path:
                wiki_paths_by_source.setdefault(str(source_id), []).append(
                    evidence_map_path
                )
        for page in state.get("pages", {}).values():
            for source_id in page.get("source_ids", []):
                wiki_paths_by_source.setdefault(str(source_id), []).append(page["path"])
        if effective_mode == "lexical":
            hits = Retriever(state).search(
                question,
                top_k,
                source_ids,
                wiki_paths_by_source,
                wiki_source_ranks,
            )
            trace = {
                "requested_mode": retrieval_mode,
                "mode": "lexical",
                "channels": (["wiki_navigation"] if wiki_navigation else [])
                + ["bm25"],
                "models": {},
                "fallback_reason": fallback_reason,
                "usage": {},
                "wiki_navigation_strategy": "wiki-page-bm25->evidence",
            }
        else:
            provider = BailianRetrievalProvider(self.root)
            hits, trace = HybridRetriever(
                state, self.vault, self.retrieval_index_path
            ).search(
                question,
                top_k,
                source_ids,
                wiki_paths_by_source,
                wiki_source_ranks,
                effective_mode,
                provider,
            )
            trace["requested_mode"] = retrieval_mode
            if fallback_reason and not trace.get("fallback_reason"):
                trace["fallback_reason"] = fallback_reason
            semantic_pages = trace.get("wiki_semantic_navigation_pages", [])
            if semantic_pages:
                fused_navigation: dict[str, dict[str, Any]] = {}
                for channel, pages in (
                    ("page_bm25", wiki_navigation),
                    ("page_embedding", semantic_pages),
                ):
                    for rank, page in enumerate(pages, 1):
                        path = str(page.get("path") or "")
                        if not path:
                            continue
                        record = fused_navigation.setdefault(
                            path,
                            {
                                **page,
                                "score": 0.0,
                                "navigation_channels": [],
                            },
                        )
                        record["score"] += 1.0 / (60 + rank)
                        record["navigation_channels"].append(channel)
                wiki_navigation = sorted(
                    fused_navigation.values(),
                    key=lambda value: (-float(value["score"]), value["path"]),
                )[: max(top_k, 8)]
                for page in wiki_navigation:
                    page["score"] = round(float(page["score"]) * 1000, 6)
                    page["navigation_channels"] = list(
                        dict.fromkeys(page["navigation_channels"])
                    )
        trace["wiki_navigation"] = wiki_navigation
        return {"hits": [hit.to_dict() for hit in hits], "retrieval": trace}

    def search(
        self,
        question: str,
        top_k: int = 5,
        source_ids: set[str] | None = None,
        retrieval_mode: str = "lexical",
    ) -> list[dict[str, Any]]:
        return self.search_with_trace(
            question, top_k, source_ids, retrieval_mode
        )["hits"]

    def _item_lookup(self, state: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        result: dict[str, tuple[str, dict[str, Any]]] = {}
        for package_id, source in state["sources"].items():
            prefix = f"{package_id}@{source['source_version']}#"
            for item in source.get("items", []):
                result[prefix + str(item["item_id"])] = (package_id, item)
        return result

    def _image_payloads(
        self,
        evidence: list[dict[str, Any]],
        state: dict[str, Any],
        preferred_asset_paths: list[str] | None = None,
    ) -> list[dict[str, str]]:
        limit = _bounded_env_int("MMWIKI_MAX_IMAGES", 4, 1, 8)
        payloads: list[dict[str, str]] = []
        used: set[str] = set()
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        for item in evidence:
            source = state["sources"][item["package_id"]]
            for asset_id in item["item"].get("asset_ids", []):
                asset = source.get("assets", {}).get(asset_id, {})
                vault_path = str(asset.get("vault_path") or "")
                if not vault_path:
                    continue
                candidates.append((vault_path, item["evidence_id"], asset))
        priority = {
            path: index for index, path in enumerate(preferred_asset_paths or [])
        }
        candidates.sort(
            key=lambda value: (priority.get(value[0], len(priority)), value[0])
        )
        for vault_path, evidence_id, asset in candidates:
            if vault_path in used:
                continue
            target = (self.vault / vault_path).resolve()
            if self.vault not in target.parents or not target.is_file():
                raise PipelineError(f"图片路径无效：{vault_path}")
            mime = str(asset.get("media_type") or mimetypes.guess_type(target.name)[0] or "")
            if not mime.startswith("image/"):
                continue
            payloads.append(
                {
                    "evidence_id": evidence_id,
                    "asset_path": vault_path,
                    "data_url": f"data:{mime};base64," + base64.b64encode(target.read_bytes()).decode("ascii"),
                }
            )
            used.add(vault_path)
            if len(payloads) >= limit:
                return payloads
        return payloads

    def query(
        self,
        question: str,
        top_k: int = 5,
        provider: str = "baseline",
        source_ids: set[str] | None = None,
        retrieval_mode: str = "auto",
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise PipelineError("问题不能为空")
        if provider not in {"baseline", "api"}:
            raise PipelineError("provider 必须是 baseline 或 api")
        started = time.perf_counter()
        search_result = self.search_with_trace(
            question, top_k, source_ids, retrieval_mode
        )
        hits = search_result["hits"]
        retrieval_trace = search_result["retrieval"]
        retrieval_queries = [question]
        rewrite_model = None
        rewrite_usage: dict[str, Any] = {}
        if provider == "api" and not hits:
            rewriter = OpenAICompatibleProvider(self.root, "build")
            rewritten = rewriter.rewrite_query(question)
            retrieval_queries = rewritten["queries"]
            rewrite_model = rewriter.model
            rewrite_usage = rewritten.get("_usage", {})
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for variant in retrieval_queries:
                variant_result = self.search_with_trace(
                    variant, top_k, source_ids, retrieval_mode
                )
                for hit in variant_result["hits"]:
                    key = (hit["source_id"], hit["chunk_id"])
                    current = merged.get(key)
                    if current is None or hit["score"] > current["score"]:
                        merged[key] = hit
            hits = sorted(
                merged.values(),
                key=lambda value: (-value["score"], value["path"], value["chunk_id"]),
            )[:top_k]
            retrieval_trace["query_rewrite_applied"] = True
        state = self._load_state()
        lookup = self._item_lookup(state)
        selected: list[dict[str, Any]] = []
        for hit in hits:
            for item_id in hit["item_ids"]:
                evidence_id = (
                    f"{hit['source_id']}@"
                    f"{state['sources'].get(hit['source_id'], {}).get('source_version', '')}"
                    f"#{item_id}"
                )
                value = lookup.get(evidence_id)
                matches = (
                    [(evidence_id, value[0], value[1])] if value is not None else []
                )
                if len(matches) != 1:
                    continue
                evidence_id, package_id, item = matches[0]
                if not any(value["evidence_id"] == evidence_id for value in selected):
                    selected.append(
                        {"evidence_id": evidence_id, "package_id": package_id, "item": item}
                    )
        evidence_candidate_count = len(selected)
        evidence_limit = _bounded_env_int("MMWIKI_MAX_EVIDENCE_ITEMS", 8, 1, 20)
        selected = selected[:evidence_limit]
        retrieval_trace["evidence_selection"] = {
            "candidate_items": evidence_candidate_count,
            "selected_items": len(selected),
            "max_items": evidence_limit,
            "truncated": evidence_candidate_count > len(selected),
        }
        matched_visual_assets = list(
            dict.fromkeys(
                str(hit.get("matched_asset_path") or "")
                for hit in hits
                if hit.get("matched_asset_path")
            )
        )
        retrieval_trace["matched_visual_assets"] = matched_visual_assets
        if not hits or not selected:
            answer = "无法从当前 Wiki 证据中确定。"
            cited: list[str] = []
            model = "not-invoked"
            answer_mode = "abstention"
            usage: dict[str, Any] = {}
        elif provider == "api":
            answer_task = "vision" if self.features.enable_vlm else "answer"
            llm = OpenAICompatibleProvider(self.root, answer_task)
            evidence_index = [
                {
                    "id": value["evidence_id"],
                    "type": value["item"]["item_type"],
                    "section": value["item"]["breadcrumb"],
                    "page": value["item"].get("page_start"),
                    "text": (
                        value["item"].get("raw_text")
                        or value["item"].get("caption")
                        or value["item"].get("search_text")
                    )[:6000],
                    "table": value["item"].get("table"),
                }
                for value in selected
            ]
            value = llm.answer(
                question,
                evidence_index,
                self._image_payloads(selected, state, matched_visual_assets)
                if self.features.enable_vlm
                else [],
            )
            answer = value["answer"]
            cited = value["evidence_refs"]
            model = llm.model
            answer_mode = (
                "multimodal_generation"
                if self.features.enable_vlm and value["answerable"]
                else ("text_generation" if value["answerable"] else "abstention")
            )
            usage = value.get("_usage", {})
        else:
            answer = "离线模式只展示召回证据，未调用视觉模型。\n\n" + "\n".join(
                f"- {hit['title']}：{hit['snippet']}" for hit in hits
            )
            cited = [value["evidence_id"] for value in selected]
            model = "deterministic-baseline"
            answer_mode = "evidence_retrieval"
            usage = {}
        valid = {value["evidence_id"] for value in selected}
        if set(cited) - valid:
            raise PipelineError("问答模型返回了检索候选之外的引用")
        citations = []
        for hit in hits:
            hit_refs = [
                ref
                for ref in cited
                if ref.startswith(f"{hit['source_id']}@")
                and any(ref.endswith(f"#{item_id}") for item_id in hit["item_ids"])
            ]
            if not hit_refs:
                continue
            citations.append({**hit, "evidence_ids": hit_refs})
        record = {
            "query_id": uuid.uuid4().hex[:16],
            "question": question,
            "answer": answer,
            "citations": citations,
            "created_at": utc_now(),
            "retriever": (
                "wiki-linked-evidence-"
                + "+".join(retrieval_trace.get("channels", ["lexical"]))
                + ("+cross-lingual-rewrite-v1" if len(retrieval_queries) > 1 else "-v1")
            ),
            "retrieval": retrieval_trace,
            "retrieval_queries": retrieval_queries,
            "query_rewrite": {
                "model": rewrite_model,
                "prompt_version": QUERY_REWRITE_PROMPT_VERSION if rewrite_model else None,
                "usage": rewrite_usage,
            },
            "provider": provider,
            "model": model,
            "prompt_version": VISION_PROMPT_VERSION if provider == "api" else "baseline-v1",
            "answer_mode": answer_mode,
            "modalities": sorted({value for hit in citations for value in hit["modalities"]}),
            "usage": usage,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        with self._state_lock:
            with self.query_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            latest_state = self._load_state()
            latest_state.setdefault("queries", []).append(record)
            self._save_state(latest_state)
            self._log("query", f"{question} · {record['retriever']} · {provider}")
        return record

    def lint(self) -> dict[str, Any]:
        state = self._load_state()
        for source_id, source in state.get("sources", {}).items():
            source.setdefault(
                "evidence_map_path",
                f"wiki/evidence/{slugify(str(source_id))}-multimodal.md",
            )
        self._write_evidence_maps(state)
        errors: list[str] = []
        warnings: list[str] = []
        evidence_ids: set[str] = set()
        vault_root = self.vault.resolve()

        def safe_vault_file(relative: Any) -> Path | None:
            raw = Path(str(relative or ""))
            if not str(relative or "") or raw.is_absolute() or ".." in raw.parts:
                return None
            target = (vault_root / raw).resolve()
            if vault_root not in target.parents:
                return None
            return target

        if not self.purpose_path.is_file():
            errors.append("Wiki Purpose 缺失：wiki-purpose.md")
        if not self.schema_path.is_file():
            errors.append("Wiki Schema 缺失：schema.md")

        for package_id, source in state.get("sources", {}).items():
            external_source = source.get("source_type") == "external_markdown"
            source_path = safe_vault_file(source.get("wiki_path"))
            if source_path is None or not source_path.is_file():
                errors.append(f"来源页缺失：{package_id}")
            prefix = f"{package_id}@{source.get('source_version', '')}#"
            source_evidence_ids = {
                prefix + str(item.get("item_id") or "")
                for item in source.get("items", [])
                if item.get("item_id")
            }
            evidence_ids.update(source_evidence_ids)
            if source_path is not None and source_path.is_file() and not external_source:
                source_content = source_path.read_text(encoding="utf-8")
                for evidence_id in source_evidence_ids:
                    if evidence_id not in source_content:
                        errors.append(
                            f"来源页缺少 Evidence：{package_id} · {evidence_id}"
                        )
            for asset_id, asset in source.get("assets", {}).items():
                target = safe_vault_file(asset.get("vault_path"))
                if target is None or not target.is_file():
                    errors.append(f"资源缺失或越界：{package_id}/{asset_id}")
            evidence_map = safe_vault_file(source.get("evidence_map_path"))
            if not external_source and (evidence_map is None or not evidence_map.is_file()):
                errors.append(f"多模态证据地图缺失：{package_id}")
        for path, page in state.get("pages", {}).items():
            target = safe_vault_file(path)
            if target is None or not target.is_file():
                errors.append(f"Wiki 页面缺失：{path}")
                content = ""
            else:
                content = target.read_text(encoding="utf-8")
            if page.get("kind") not in {"concept", "entity", "analysis", "external_markdown"}:
                errors.append(f"Wiki 页面 kind 无效：{path}")
            required = ("source_ids", "source_versions", "evidence_ids")
            for field in required:
                if not page.get(field):
                    errors.append(f"Wiki 页面缺少 {field}：{path}")
            missing_evidence = set(page.get("evidence_ids", [])) - evidence_ids
            if missing_evidence:
                errors.append(f"Wiki 页面引用无效 Evidence：{path} · {sorted(missing_evidence)}")
            missing_sources = set(page.get("source_ids", [])) - set(
                state.get("sources", {})
            )
            if missing_sources:
                errors.append(
                    f"Wiki 页面引用不存在来源：{path} · {sorted(missing_sources)}"
                )
            for evidence_id in page.get("evidence_ids", []) if page.get("kind") != "external_markdown" else []:
                if content and str(evidence_id) not in content:
                    errors.append(
                        f"Wiki 页面正文缺少 Evidence：{path} · {evidence_id}"
                    )
            if not str(page.get("summary") or "").strip():
                warnings.append(f"Wiki 页面缺少摘要：{path}")
            if content.count("\n---\n") > 1:
                warnings.append(f"Wiki 页面疑似包含重复 frontmatter：{path}")
            if content.casefold().count(f"# {page.get('title', '')}".casefold()) > 1:
                warnings.append(f"Wiki 页面疑似包含重复一级标题：{path}")
        coverage = self._wiki_coverage(state)
        if coverage["uncovered_source_ids"]:
            warnings.append(
                "稳定知识页尚未覆盖全部来源："
                + ", ".join(coverage["uncovered_source_ids"])
            )
        graph = self._wiki_graph(state)
        self._write_graph_health(graph)
        maintenance = self._write_maintenance(state, graph)
        if maintenance["stale_pages"]:
            warnings.append(
                f"存在 {len(maintenance['stale_pages'])} 个引用旧来源版本的知识页"
            )
        if graph["orphans"]:
            warnings.append(
                f"存在 {len(graph['orphans'])} 个孤立稳定知识页，请补充有意义的 WikiLink"
            )
        if graph["wanted_pages"]:
            warnings.append(
                f"存在 {len(graph['wanted_pages'])} 个待创建或断链 WikiLink 目标"
            )
        if graph["duplicate_titles"]:
            warnings.append(
                f"存在 {len(graph['duplicate_titles'])} 组重复页面标题"
            )
        result = {
            "status": "passed" if not errors else "failed",
            "sources": len(state.get("sources", {})),
            "pages": len(state.get("pages", {})),
            "wiki_coverage": coverage,
            "graph": {
                **graph["stats"],
                "orphans": graph["orphans"],
                "wanted_pages": graph["wanted_pages"],
                "duplicate_titles": graph["duplicate_titles"],
                "full_report": self.graph_path.relative_to(self.vault).as_posix(),
            },
            "maintenance": {
                "status": maintenance["status"],
                "stale_pages": maintenance["stale_pages"],
                "review_pages": maintenance["review_pages"],
                "full_report": self.maintenance_path.relative_to(self.vault).as_posix(),
            },
            "warnings": warnings,
            "errors": errors,
        }
        self._log(
            "lint",
            f"{result['status']} · {len(errors)} errors · {len(warnings)} warnings",
        )
        return result
