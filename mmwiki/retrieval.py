from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover - 系统 Python 证书正常时不需要该依赖
    certifi = None

from .models import SearchHit
from .provider import ProviderError, read_dotenv
from .search import Retriever, make_search_hit


RETRIEVAL_INDEX_VERSION = "mmwiki-retrieval-0.2"
LEGACY_RETRIEVAL_INDEX_VERSION = "mmwiki-retrieval-0.1"
RETRIEVAL_MODES = ("lexical", "hybrid", "multimodal")


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
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
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def _service_root(base_url: str) -> str:
    for suffix in ("/compatible-mode/v1", "/compatible-api/v1", "/api/v1"):
        if base_url.endswith(suffix):
            return base_url[: -len(suffix)]
    return base_url.rstrip("/")


def _usage_total(target: dict[str, int], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + int(value)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def image_data_url(path: Path, media_type: str = "") -> str:
    mime = media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


class BailianRetrievalProvider:
    """百炼文本与多模态检索接口，保持为可替换的只读 Provider。"""

    def __init__(self, root: Path):
        values = read_dotenv(root / ".env")

        def setting(name: str, default: str = "") -> str:
            return os.environ.get(name, values.get(name, default)).strip()

        base_url = setting("MMWIKI_API_BASE_URL").rstrip("/")
        service_root = _service_root(base_url)
        self.key = setting("MMWIKI_API_KEY")
        self.timeout = int(setting("MMWIKI_TIMEOUT", "60"))
        self.text_embedding_model = setting(
            "MMWIKI_TEXT_EMBEDDING_MODEL", "qwen3.7-text-embedding"
        )
        self.text_rerank_model = setting("MMWIKI_TEXT_RERANK_MODEL", "qwen3-rerank")
        self.vl_embedding_model = setting(
            "MMWIKI_VL_EMBEDDING_MODEL", "qwen3-vl-embedding"
        )
        self.vl_rerank_model = setting("MMWIKI_VL_RERANK_MODEL", "qwen3-vl-rerank")
        dimension = setting("MMWIKI_EMBEDDING_DIMENSION", "1024")
        self.dimension = int(dimension) if dimension else None
        self.text_embedding_url = setting(
            "MMWIKI_TEXT_EMBEDDING_URL",
            f"{base_url}/embeddings" if base_url else "",
        )
        self.text_rerank_url = setting(
            "MMWIKI_TEXT_RERANK_URL",
            f"{service_root}/compatible-api/v1/reranks" if service_root else "",
        )
        self.vl_embedding_url = setting(
            "MMWIKI_VL_EMBEDDING_URL",
            (
                f"{service_root}/api/v1/services/embeddings/"
                "multimodal-embedding/multimodal-embedding"
            )
            if service_root
            else "",
        )
        self.vl_rerank_url = setting(
            "MMWIKI_VL_RERANK_URL",
            (
                f"{service_root}/api/v1/services/rerank/"
                "text-rerank/text-rerank"
            )
            if service_root
            else "",
        )

    @property
    def text_configured(self) -> bool:
        return bool(
            self.key
            and self.text_embedding_url
            and self.text_rerank_url
            and self.text_embedding_model
            and self.text_rerank_model
        )

    @property
    def multimodal_configured(self) -> bool:
        return bool(
            self.key
            and self.vl_embedding_url
            and self.vl_rerank_url
            and self.vl_embedding_model
            and self.vl_rerank_model
        )

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.key or not url:
            raise ProviderError("检索模型 API 尚未配置")
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        context = (
            ssl.create_default_context(cafile=certifi.where())
            if certifi is not None
            else ssl.create_default_context()
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=context
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(f"检索模型 API 返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"检索模型 API 调用失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("检索模型 API 返回格式错误")
        if payload.get("code") and not payload.get("data") and not payload.get("output"):
            raise ProviderError(f"检索模型 API 返回错误：{payload.get('message') or payload['code']}")
        return payload

    def text_embeddings(self, texts: list[str]) -> tuple[list[list[float]], dict[str, int]]:
        vectors: list[list[float]] = []
        usage: dict[str, int] = {}
        for offset in range(0, len(texts), 10):
            batch = texts[offset : offset + 10]
            body: dict[str, Any] = {
                "model": self.text_embedding_model,
                "input": batch,
            }
            if self.dimension:
                body["dimensions"] = self.dimension
            payload = self._post(self.text_embedding_url, body)
            data = payload.get("data")
            if not isinstance(data, list):
                raise ProviderError("文本向量模型没有返回 data 数组")
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            batch_vectors = [item.get("embedding") for item in ordered]
            if len(batch_vectors) != len(batch) or any(
                not isinstance(vector, list) for vector in batch_vectors
            ):
                raise ProviderError("文本向量数量与输入不一致")
            vectors.extend([[float(value) for value in vector] for vector in batch_vectors])
            _usage_total(usage, payload.get("usage"))
        return vectors, usage

    def text_rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        payload = self._post(
            self.text_rerank_url,
            {
                "model": self.text_rerank_model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
                "instruct": (
                    "Given a multimodal Wiki question, retrieve passages that directly "
                    "support the answer with source-grounded evidence."
                ),
            },
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise ProviderError("文本重排模型没有返回 results 数组")
        usage: dict[str, int] = {}
        _usage_total(usage, payload.get("usage"))
        return results, usage

    def multimodal_embedding(
        self, contents: list[dict[str, str]], fused: bool
    ) -> tuple[list[float], dict[str, int]]:
        parameters: dict[str, Any] = {}
        if fused:
            parameters["enable_fusion"] = True
        if self.dimension:
            parameters["dimension"] = self.dimension
        body: dict[str, Any] = {
            "model": self.vl_embedding_model,
            "input": {"contents": contents},
        }
        if parameters:
            body["parameters"] = parameters
        payload = self._post(self.vl_embedding_url, body)
        output = payload.get("output")
        embeddings = output.get("embeddings") if isinstance(output, dict) else None
        if not isinstance(embeddings, list) or not embeddings:
            raise ProviderError("多模态向量模型没有返回 embeddings")
        vector = embeddings[0].get("embedding")
        if not isinstance(vector, list):
            raise ProviderError("多模态向量格式错误")
        usage: dict[str, int] = {}
        _usage_total(usage, payload.get("usage"))
        return [float(value) for value in vector], usage

    def multimodal_rerank(
        self, query: str, documents: list[dict[str, str]], top_n: int
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        payload = self._post(
            self.vl_rerank_url,
            {
                "model": self.vl_rerank_model,
                "input": {
                    "query": {"text": query},
                    "documents": documents,
                },
                "parameters": {
                    "return_documents": False,
                    "top_n": min(top_n, len(documents)),
                    "instruct": (
                        "Rank text and image evidence by whether it directly answers "
                        "the multimodal Wiki question."
                    ),
                },
            },
        )
        output = payload.get("output")
        results = output.get("results") if isinstance(output, dict) else None
        if not isinstance(results, list):
            raise ProviderError("多模态重排模型没有返回 results 数组")
        usage: dict[str, int] = {}
        _usage_total(usage, payload.get("usage"))
        return results, usage


def _source_versions(state: dict[str, Any]) -> dict[str, str]:
    return {
        str(source_id): str(source.get("source_version") or "")
        for source_id, source in state.get("sources", {}).items()
    }


def _source_fingerprints(state: dict[str, Any]) -> dict[str, str]:
    """Fingerprint the active representation, not only the immutable source version."""
    result: dict[str, str] = {}
    for source_id, source in state.get("sources", {}).items():
        payload = {
            "source_version": str(source.get("source_version") or ""),
            "representation": str(source.get("representation") or "legacy"),
            "chunks": [
                {
                    "chunk_id": str(chunk.get("chunk_id") or ""),
                    "text": str(chunk.get("text") or ""),
                    "asset_ids": list(map(str, chunk.get("asset_ids", []))),
                }
                for chunk in source.get("chunks", [])
            ],
            "assets": {
                str(asset_id): {
                    "sha256": str(asset.get("sha256") or ""),
                    "vault_path": str(asset.get("vault_path") or ""),
                }
                for asset_id, asset in sorted(source.get("assets", {}).items())
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        result[str(source_id)] = hashlib.sha256(encoded).hexdigest()
    return result


def _document_text(source: dict[str, Any], chunk: dict[str, Any]) -> str:
    return "\n".join(
        value
        for value in (
            str(source.get("title") or ""),
            str(chunk.get("breadcrumb") or ""),
            str(chunk.get("text") or ""),
        )
        if value
    )[:8000]


def _wiki_page_text(title: str, summary: str, content: str) -> str:
    """Create a compact page representation without duplicating raw Evidence."""
    body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)
    body = re.sub(
        r"<!-- mmwiki:multimodal-evidence:start -->.*?"
        r"<!-- mmwiki:multimodal-evidence:end -->",
        "",
        body,
        flags=re.DOTALL,
    )
    return "\n".join(value for value in (title, summary, body.strip()) if value)[:12000]


def _wiki_documents(state: dict[str, Any], vault: Path) -> dict[str, dict[str, Any]]:
    """Return real Wiki pages as independently indexable navigation records."""
    candidates: list[dict[str, Any]] = []
    for source_id, source in state.get("sources", {}).items():
        candidates.append(
            {
                "path": str(source.get("wiki_path") or ""),
                "title": str(source.get("title") or source_id),
                "summary": "原始来源的可追溯文本视图。",
                "kind": "source",
                "source_ids": [str(source_id)],
            }
        )
        evidence_map_path = str(source.get("evidence_map_path") or "")
        if evidence_map_path:
            candidates.append(
                {
                    "path": evidence_map_path,
                    "title": f"{source.get('title') or source_id} · 多模态证据地图",
                    "summary": "按结构、页码和模态组织的原始 Evidence 地图。",
                    "kind": "evidence-map",
                    "source_ids": [str(source_id)],
                }
            )
    for page in state.get("pages", {}).values():
        candidates.append(
            {
                "path": str(page.get("path") or ""),
                "title": str(page.get("title") or ""),
                "summary": str(page.get("summary") or ""),
                "kind": str(page.get("kind") or "analysis"),
                "source_ids": [str(value) for value in page.get("source_ids", [])],
            }
        )

    result: dict[str, dict[str, Any]] = {}
    vault_root = vault.resolve()
    for candidate in candidates:
        relative = candidate["path"]
        raw_path = Path(relative)
        if not relative or raw_path.is_absolute() or ".." in raw_path.parts:
            continue
        target = (vault_root / raw_path).resolve()
        if vault_root not in target.parents or not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text = _wiki_page_text(candidate["title"], candidate["summary"], content)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        result[relative] = {**candidate, "content_hash": content_hash, "text": text}
    return result


def _wiki_fingerprint(documents: dict[str, dict[str, Any]]) -> str:
    payload = {
        path: {
            "content_hash": document["content_hash"],
            "source_ids": document["source_ids"],
        }
        for path, document in sorted(documents.items())
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RetrievalIndex:
    def __init__(self, path: Path, vault: Path):
        self.path = path
        self.vault = vault.resolve()

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def status(
        self, state: dict[str, Any], provider: BailianRetrievalProvider
    ) -> dict[str, Any]:
        value = self.load()
        current_sources = _source_versions(state)
        current_fingerprints = _source_fingerprints(state)
        current_wiki_documents = _wiki_documents(state, self.vault)
        current_wiki_fingerprint = _wiki_fingerprint(current_wiki_documents)
        fresh = (
            value.get("schema_version") == RETRIEVAL_INDEX_VERSION
            and value.get("sources") == current_sources
            and value.get("source_fingerprints") == current_fingerprints
        )
        text = value.get("text") if isinstance(value.get("text"), dict) else {}
        visual = value.get("visual") if isinstance(value.get("visual"), dict) else {}
        wiki = value.get("wiki") if isinstance(value.get("wiki"), dict) else {}
        return {
            "path": str(self.path),
            "fresh": fresh,
            "text_ready": bool(
                fresh
                and text.get("model") == provider.text_embedding_model
                and text.get("records")
            ),
            "visual_ready": bool(
                fresh
                and visual.get("model") == provider.vl_embedding_model
                and visual.get("records")
            ),
            "wiki_semantic_ready": bool(
                fresh
                and wiki.get("model") == provider.text_embedding_model
                and wiki.get("fingerprint") == current_wiki_fingerprint
                and wiki.get("records")
            ),
            "text_model": text.get("model") or provider.text_embedding_model,
            "text_rerank_model": provider.text_rerank_model,
            "visual_model": visual.get("model") or provider.vl_embedding_model,
            "visual_rerank_model": provider.vl_rerank_model,
            "text_records": len(text.get("records", [])),
            "visual_records": len(visual.get("records", [])),
            "wiki_records": len(wiki.get("records", [])),
            "wiki_fingerprint_ready": bool(
                wiki.get("fingerprint") == current_wiki_fingerprint
            ),
            "source_fingerprints_ready": bool(
                value.get("source_fingerprints") == current_fingerprints
            ),
            "created_at": value.get("created_at"),
        }

    def migrate_legacy(
        self, state: dict[str, Any], provider: BailianRetrievalProvider
    ) -> dict[str, Any]:
        """Upgrade v0.1 metadata locally without recomputing or exporting vectors."""
        value = self.load()
        if value.get("schema_version") == RETRIEVAL_INDEX_VERSION:
            return self.status(state, provider) | {"status": "already_current"}
        if value.get("schema_version") != LEGACY_RETRIEVAL_INDEX_VERSION:
            raise ProviderError("现有索引不是可迁移的 mmwiki-retrieval-0.1")

        current_versions = _source_versions(state)
        if value.get("sources") != current_versions:
            raise ProviderError("旧索引来源版本与当前状态不一致，必须重新构建")
        text = value.get("text") if isinstance(value.get("text"), dict) else {}
        visual = value.get("visual") if isinstance(value.get("visual"), dict) else {}
        if text.get("model") != provider.text_embedding_model:
            raise ProviderError("旧索引文本模型与当前配置不一致，必须重新构建")
        if visual.get("model") not in (None, provider.vl_embedding_model):
            raise ProviderError("旧索引视觉模型与当前配置不一致，必须重新构建")

        sources = state.get("sources", {})
        expected_text = {
            (str(source_id), str(chunk.get("chunk_id") or ""))
            for source_id, source in sources.items()
            for chunk in source.get("chunks", [])
        }
        indexed_text = {
            (
                str(record.get("source_id") or ""),
                str(record.get("chunk_id") or ""),
            )
            for record in text.get("records", [])
            if isinstance(record, dict) and isinstance(record.get("vector"), list)
        }
        if indexed_text != expected_text:
            raise ProviderError("旧索引文本记录与当前 Chunk 不完整匹配，必须重新构建")

        expected_visual: set[tuple[str, str, str, str]] = set()
        for source_id, source in sources.items():
            for chunk in source.get("chunks", []):
                for raw_asset_id in chunk.get("asset_ids", []):
                    asset_id = str(raw_asset_id)
                    asset = source.get("assets", {}).get(raw_asset_id)
                    if asset is None:
                        asset = source.get("assets", {}).get(asset_id, {})
                    vault_path = str(asset.get("vault_path") or "")
                    mime = str(asset.get("media_type") or "")
                    target = (self.vault / vault_path).resolve()
                    detected = mime or mimetypes.guess_type(target.name)[0] or ""
                    if (
                        vault_path
                        and self.vault in target.parents
                        and target.is_file()
                        and detected.startswith("image/")
                    ):
                        expected_visual.add(
                            (
                                str(source_id),
                                str(chunk.get("chunk_id") or ""),
                                asset_id,
                                vault_path,
                            )
                        )
        indexed_visual = {
            (
                str(record.get("source_id") or ""),
                str(record.get("chunk_id") or ""),
                str(record.get("asset_id") or ""),
                str(record.get("asset_path") or ""),
            )
            for record in visual.get("records", [])
            if isinstance(record, dict) and isinstance(record.get("vector"), list)
        }
        if indexed_visual != expected_visual:
            raise ProviderError("旧索引视觉记录与当前资源不完整匹配，必须重新构建")

        value["schema_version"] = RETRIEVAL_INDEX_VERSION
        value["source_fingerprints"] = _source_fingerprints(state)
        value["migrated_at"] = datetime.now(timezone.utc).isoformat()
        value["migrated_from"] = LEGACY_RETRIEVAL_INDEX_VERSION
        _atomic_write_json(self.path, value)
        return self.status(state, provider) | {
            "status": "migrated",
            "external_api_calls": 0,
            "preserved_text_records": len(indexed_text),
            "preserved_visual_records": len(indexed_visual),
        }

    def build_wiki_pages(
        self, state: dict[str, Any], provider: BailianRetrievalProvider
    ) -> dict[str, Any]:
        """Backfill only the page-level Wiki index and preserve Evidence vectors."""
        if not provider.text_configured:
            raise ProviderError("文本向量接口未配置")
        value = self.load()
        current_versions = _source_versions(state)
        current_fingerprints = _source_fingerprints(state)
        if value.get("schema_version") != RETRIEVAL_INDEX_VERSION:
            raise ProviderError("现有索引版本不兼容，请先迁移或构建基础索引")
        if (
            value.get("sources") != current_versions
            or value.get("source_fingerprints") != current_fingerprints
        ):
            raise ProviderError("现有 Evidence 索引与当前来源不一致，不能只回填 Wiki 页面")
        text = value.get("text") if isinstance(value.get("text"), dict) else {}
        if (
            text.get("model") != provider.text_embedding_model
            or not text.get("records")
        ):
            raise ProviderError("基础文本 Evidence 索引缺失或模型不一致")

        current_wiki = _wiki_documents(state, self.vault)
        existing_wiki = (
            value.get("wiki") if isinstance(value.get("wiki"), dict) else {}
        )
        existing_records = (
            existing_wiki.get("records", [])
            if existing_wiki.get("model") in (None, provider.text_embedding_model)
            else []
        )
        reusable: dict[str, dict[str, Any]] = {}
        for record in existing_records:
            if not isinstance(record, dict):
                continue
            path = str(record.get("path") or "")
            document = current_wiki.get(path)
            if (
                document
                and record.get("content_hash") == document["content_hash"]
                and isinstance(record.get("vector"), list)
            ):
                reusable[path] = record

        missing_paths = sorted(set(current_wiki) - set(reusable))
        vectors, usage = (
            provider.text_embeddings(
                [current_wiki[path]["text"] for path in missing_paths]
            )
            if missing_paths
            else ([], {})
        )
        new_records = {
            path: {
                **{
                    key: field
                    for key, field in current_wiki[path].items()
                    if key != "text"
                },
                "vector": vector,
            }
            for path, vector in zip(missing_paths, vectors)
        }
        if len(new_records) != len(missing_paths):
            raise ProviderError("Wiki 页面向量数量与待索引页面不一致")

        value["wiki"] = {
            "model": provider.text_embedding_model,
            "fingerprint": _wiki_fingerprint(current_wiki),
            "records": [
                (reusable | new_records)[path] for path in sorted(current_wiki)
            ],
            "usage": usage,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(self.path, value)
        return self.status(state, provider) | {
            "status": "wiki_page_index_built",
            "index_scope": "wiki_pages_only",
            "wiki_usage": usage,
            "reused_wiki_records": len(reusable),
            "new_wiki_records": len(new_records),
            "preserved_text_records": len(text.get("records", [])),
            "preserved_visual_records": len(
                value.get("visual", {}).get("records", [])
                if isinstance(value.get("visual"), dict)
                else []
            ),
        }

    def build(
        self,
        state: dict[str, Any],
        provider: BailianRetrievalProvider,
        include_visual: bool,
        source_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        if not provider.text_configured:
            raise ProviderError("文本向量与重排接口未配置")
        sources = state.get("sources", {})
        active_source_ids = {str(source_id) for source_id in sources}
        selected_source_ids = (
            active_source_ids if source_ids is None else {str(value) for value in source_ids}
        )
        unknown_source_ids = selected_source_ids - active_source_ids
        if unknown_source_ids:
            raise ProviderError(
                "增量索引包含未知来源：" + ", ".join(sorted(unknown_source_ids))
            )

        incremental = source_ids is not None
        retained_source_ids = active_source_ids - selected_source_ids
        existing = self.load() if incremental else {}
        current_versions = _source_versions(state)
        current_fingerprints = _source_fingerprints(state)
        existing_versions: dict[str, str] = {}
        existing_fingerprints: dict[str, str] = {}
        existing_text_records: list[dict[str, Any]] = []
        existing_visual_records: list[dict[str, Any]] = []
        existing_wiki_records: list[dict[str, Any]] = []
        if incremental:
            if existing.get("schema_version") != RETRIEVAL_INDEX_VERSION:
                raise ProviderError("现有索引版本不兼容，无法安全增量合并")
            existing_versions = (
                existing.get("sources")
                if isinstance(existing.get("sources"), dict)
                else {}
            )
            existing_fingerprints = (
                existing.get("source_fingerprints")
                if isinstance(existing.get("source_fingerprints"), dict)
                else {}
            )
            stale = [
                source_id
                for source_id in sorted(retained_source_ids)
                if existing_versions.get(source_id) != current_versions.get(source_id)
                or existing_fingerprints.get(source_id)
                != current_fingerprints.get(source_id)
            ]
            if stale:
                raise ProviderError(
                    "未选来源的索引缺失或已过期，需一并重建："
                    + ", ".join(stale)
                )

            existing_text = (
                existing.get("text") if isinstance(existing.get("text"), dict) else {}
            )
            if existing_text.get("model") != provider.text_embedding_model:
                raise ProviderError("现有文本索引模型不一致，无法安全增量合并")
            existing_text_records = [
                record
                for record in existing_text.get("records", [])
                if isinstance(record, dict)
            ]
            existing_wiki = (
                existing.get("wiki") if isinstance(existing.get("wiki"), dict) else {}
            )
            if existing_wiki.get("model") in (None, provider.text_embedding_model):
                existing_wiki_records = [
                    record
                    for record in existing_wiki.get("records", [])
                    if isinstance(record, dict)
                ]
            expected_retained_chunks = {
                (source_id, str(chunk.get("chunk_id") or ""))
                for source_id in retained_source_ids
                for chunk in sources[source_id].get("chunks", [])
            }
            indexed_retained_chunks = {
                (
                    str(record.get("source_id") or ""),
                    str(record.get("chunk_id") or ""),
                )
                for record in existing_text_records
                if str(record.get("source_id") or "") in retained_source_ids
            }
            if indexed_retained_chunks != expected_retained_chunks:
                raise ProviderError("未选来源的文本索引记录不完整，拒绝标记为最新")

            if include_visual:
                existing_visual = (
                    existing.get("visual")
                    if isinstance(existing.get("visual"), dict)
                    else {}
                )
                existing_visual_model = existing_visual.get("model")
                if existing_visual_model not in (None, provider.vl_embedding_model):
                    raise ProviderError("现有视觉索引模型不一致，无法安全增量合并")
                existing_visual_records = [
                    record
                    for record in existing_visual.get("records", [])
                    if isinstance(record, dict)
                ]

        current_text: dict[tuple[str, str], tuple[dict[str, str], str]] = {}
        for source_id, source in sources.items():
            for chunk in source.get("chunks", []):
                key = (str(source_id), str(chunk.get("chunk_id") or ""))
                current_text[key] = (
                    {"source_id": key[0], "chunk_id": key[1]},
                    _document_text(source, chunk),
                )

        reusable_text: dict[tuple[str, str], dict[str, Any]] = {}
        for record in existing_text_records:
            key = (
                str(record.get("source_id") or ""),
                str(record.get("chunk_id") or ""),
            )
            source_id = key[0]
            if key not in current_text or not isinstance(record.get("vector"), list):
                continue
            if source_id in retained_source_ids or (
                source_id in selected_source_ids
                and existing_versions.get(source_id) == current_versions.get(source_id)
            ):
                reusable_text[key] = record

        missing_text_keys = sorted(
            key
            for key in current_text
            if key[0] in selected_source_ids and key not in reusable_text
        )
        text_inputs = [current_text[key][1] for key in missing_text_keys]
        vectors, text_usage = (
            provider.text_embeddings(text_inputs) if text_inputs else ([], {})
        )
        new_text_records = {
            key: {**current_text[key][0], "vector": vector}
            for key, vector in zip(missing_text_keys, vectors)
        }
        if len(new_text_records) != len(missing_text_keys):
            raise ProviderError("文本向量数量与待索引记录不一致")
        text_records = [
            (reusable_text | new_text_records)[key]
            for key in sorted(current_text)
            if key in reusable_text or key in new_text_records
        ]
        if len(text_records) != len(current_text):
            raise ProviderError("文本索引记录不完整，拒绝写入")

        # Wiki pages have their own semantic navigation index.  They are kept
        # separate from chunk vectors so a query can choose the knowledge page
        # first and only then descend to raw Evidence.
        current_wiki = _wiki_documents(state, self.vault)
        reusable_wiki: dict[str, dict[str, Any]] = {}
        for record in existing_wiki_records:
            path = str(record.get("path") or "")
            document = current_wiki.get(path)
            if (
                document
                and record.get("content_hash") == document["content_hash"]
                and isinstance(record.get("vector"), list)
            ):
                reusable_wiki[path] = record
        missing_wiki_paths = sorted(set(current_wiki) - set(reusable_wiki))
        wiki_vectors, wiki_usage = (
            provider.text_embeddings(
                [current_wiki[path]["text"] for path in missing_wiki_paths]
            )
            if missing_wiki_paths
            else ([], {})
        )
        new_wiki_records = {
            path: {
                **{
                    key: value
                    for key, value in current_wiki[path].items()
                    if key != "text"
                },
                "vector": vector,
            }
            for path, vector in zip(missing_wiki_paths, wiki_vectors)
        }
        if len(new_wiki_records) != len(missing_wiki_paths):
            raise ProviderError("Wiki 页面向量数量与待索引页面不一致")
        wiki_records = [
            (reusable_wiki | new_wiki_records)[path]
            for path in sorted(current_wiki)
        ]

        current_visual: dict[
            tuple[str, str, str, str], tuple[str, Path, str]
        ] = {}
        for source_id, source in sources.items():
            for chunk in source.get("chunks", []):
                text = _document_text(source, chunk)[:3000]
                for raw_asset_id in chunk.get("asset_ids", []):
                    asset_id = str(raw_asset_id)
                    asset = source.get("assets", {}).get(raw_asset_id)
                    if asset is None:
                        asset = source.get("assets", {}).get(asset_id, {})
                    vault_path = str(asset.get("vault_path") or "")
                    target = (self.vault / vault_path).resolve()
                    if (
                        not vault_path
                        or self.vault not in target.parents
                        or not target.is_file()
                    ):
                        continue
                    mime = str(asset.get("media_type") or "")
                    detected_mime = mime or mimetypes.guess_type(target.name)[0] or ""
                    if not detected_mime.startswith("image/"):
                        continue
                    key = (
                        str(source_id),
                        str(chunk.get("chunk_id") or ""),
                        asset_id,
                        vault_path,
                    )
                    current_visual[key] = (text, target, detected_mime)

        reusable_visual: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        if include_visual:
            for record in existing_visual_records:
                key = (
                    str(record.get("source_id") or ""),
                    str(record.get("chunk_id") or ""),
                    str(record.get("asset_id") or ""),
                    str(record.get("asset_path") or ""),
                )
                source_id = key[0]
                if key not in current_visual or not isinstance(record.get("vector"), list):
                    continue
                if source_id in retained_source_ids or (
                    source_id in selected_source_ids
                    and existing_versions.get(source_id) == current_versions.get(source_id)
                ):
                    reusable_visual[key] = record

            expected_retained_visual = {
                key for key in current_visual if key[0] in retained_source_ids
            }
            indexed_retained_visual = {
                key for key in reusable_visual if key[0] in retained_source_ids
            }
            if indexed_retained_visual != expected_retained_visual:
                raise ProviderError("未选来源的视觉索引记录不完整，需一并重建")

        visual_records: list[dict[str, Any]] = []
        visual_usage: dict[str, int] = {}
        missing_visual_keys: list[tuple[str, str, str, str]] = []
        if include_visual:
            if not provider.multimodal_configured:
                raise ProviderError("多模态向量与重排接口未配置")
            missing_visual_keys = sorted(
                key
                for key in current_visual
                if key[0] in selected_source_ids and key not in reusable_visual
            )
            new_visual_records: dict[
                tuple[str, str, str, str], dict[str, Any]
            ] = {}
            for key in missing_visual_keys:
                text, target, mime = current_visual[key]
                vector, usage = provider.multimodal_embedding(
                    [{"text": text}, {"image": image_data_url(target, mime)}],
                    fused=True,
                )
                _usage_total(visual_usage, usage)
                new_visual_records[key] = {
                    "source_id": key[0],
                    "chunk_id": key[1],
                    "asset_id": key[2],
                    "asset_path": key[3],
                    "vector": vector,
                }
            visual_records = [
                (reusable_visual | new_visual_records)[key]
                for key in sorted(current_visual)
                if key in reusable_visual or key in new_visual_records
            ]
            if len(visual_records) != len(current_visual):
                raise ProviderError("视觉索引记录不完整，拒绝写入")

        value = {
            "schema_version": RETRIEVAL_INDEX_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sources": current_versions,
            "source_fingerprints": current_fingerprints,
            "text": {
                "model": provider.text_embedding_model,
                "records": text_records,
                "usage": text_usage,
            },
            "wiki": {
                "model": provider.text_embedding_model,
                "fingerprint": _wiki_fingerprint(current_wiki),
                "records": wiki_records,
                "usage": wiki_usage,
            },
            "visual": {
                "model": provider.vl_embedding_model if include_visual else None,
                "records": visual_records,
                "usage": visual_usage,
            },
        }
        _atomic_write_json(self.path, value)
        return self.status(state, provider) | {
            "indexed_source_ids": sorted(selected_source_ids),
            "incremental": incremental,
            "text_usage": text_usage,
            "wiki_usage": wiki_usage,
            "visual_usage": visual_usage,
            "reused_text_records": len(reusable_text),
            "new_text_records": len(new_text_records),
            "reused_wiki_records": len(reusable_wiki),
            "new_wiki_records": len(new_wiki_records),
            "reused_visual_records": len(reusable_visual),
            "new_visual_records": len(missing_visual_keys),
        }


class HybridRetriever:
    def __init__(self, state: dict[str, Any], vault: Path, index_path: Path):
        self.state = state
        self.vault = vault.resolve()
        self.index = RetrievalIndex(index_path, vault)
        self.chunk_lookup: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for source_id, source in state.get("sources", {}).items():
            for chunk in source.get("chunks", []):
                self.chunk_lookup[(str(source_id), str(chunk.get("chunk_id") or ""))] = (
                    source,
                    chunk,
                )

    def _rank_vectors(
        self,
        vector: list[float],
        records: list[dict[str, Any]],
        query: str,
        source_ids: set[str] | None,
        wiki_paths_by_source: dict[str, list[str]],
        limit: int,
        channel: str,
    ) -> list[SearchHit]:
        ranked: list[tuple[float, dict[str, Any]]] = []
        for record in records:
            source_id = str(record.get("source_id") or "")
            if source_ids is not None and source_id not in source_ids:
                continue
            candidate = record.get("vector")
            if not isinstance(candidate, list):
                continue
            score = cosine_similarity(vector, [float(value) for value in candidate])
            ranked.append((score, record))
        ranked.sort(key=lambda value: value[0], reverse=True)
        hits: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        for score, record in ranked:
            key = (str(record.get("source_id") or ""), str(record.get("chunk_id") or ""))
            if key in seen or key not in self.chunk_lookup:
                continue
            seen.add(key)
            source, chunk = self.chunk_lookup[key]
            hits.append(
                make_search_hit(
                    key[0],
                    source,
                    chunk,
                    query,
                    score,
                    wiki_paths_by_source.get(key[0], []),
                    [channel],
                    {channel: round(score, 6)},
                    str(record.get("asset_id") or ""),
                    str(record.get("asset_path") or ""),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    @staticmethod
    def _rank_wiki_pages(
        vector: list[float],
        records: list[dict[str, Any]],
        source_ids: set[str] | None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for record in records:
            page_source_ids = [str(value) for value in record.get("source_ids", [])]
            if source_ids is not None and not source_ids.intersection(page_source_ids):
                continue
            candidate = record.get("vector")
            if not isinstance(candidate, list):
                continue
            score = cosine_similarity(vector, [float(value) for value in candidate])
            ranked.append(
                {
                    "path": str(record.get("path") or ""),
                    "title": str(record.get("title") or ""),
                    "summary": str(record.get("summary") or ""),
                    "kind": str(record.get("kind") or "analysis"),
                    "source_ids": page_source_ids,
                    "score": round(score, 6),
                    "navigation_stage": "wiki-page-embedding",
                    "score_breakdown": {"page_embedding": round(score, 6)},
                }
            )
        ranked.sort(key=lambda value: (-value["score"], value["path"]))
        return ranked[: max(1, min(limit, 20))]

    @staticmethod
    def _rank_wiki_sources(
        vector: list[float],
        records: list[dict[str, Any]],
        source_ids: set[str] | None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Aggregate chunk-vector evidence into a coarse semantic source route."""
        scores: dict[str, list[float]] = {}
        for record in records:
            source_id = str(record.get("source_id") or "")
            if not source_id or (source_ids is not None and source_id not in source_ids):
                continue
            candidate = record.get("vector")
            if not isinstance(candidate, list):
                continue
            scores.setdefault(source_id, []).append(
                cosine_similarity(vector, [float(value) for value in candidate])
            )
        ranked = []
        for source_id, values in scores.items():
            strongest = sorted(values, reverse=True)[:3]
            ranked.append(
                {
                    "source_id": source_id,
                    "score": round(sum(strongest) / len(strongest), 6),
                }
            )
        ranked.sort(key=lambda value: (-value["score"], value["source_id"]))
        return ranked[: max(1, min(limit, 8))]

    @staticmethod
    def _wiki_pages_to_sources(
        pages: list[dict[str, Any]], limit: int = 3
    ) -> list[dict[str, Any]]:
        scores: dict[str, list[float]] = {}
        for page in pages:
            for source_id in page.get("source_ids", []):
                scores.setdefault(str(source_id), []).append(float(page.get("score") or 0.0))
        ranked = [
            {"source_id": source_id, "score": round(max(values), 6)}
            for source_id, values in scores.items()
            if values
        ]
        ranked.sort(key=lambda value: (-value["score"], value["source_id"]))
        return ranked[: max(1, min(limit, 8))]

    @staticmethod
    def _merge_wiki_source_ranks(
        lexical_ranks: dict[str, int] | None,
        semantic: list[dict[str, Any]],
        limit: int = 3,
    ) -> dict[str, int]:
        scores: dict[str, float] = {}
        for source_id, rank in (lexical_ranks or {}).items():
            scores[source_id] = scores.get(source_id, 0.0) + 1.0 / (60 + rank)
        for rank, value in enumerate(semantic, 1):
            source_id = str(value["source_id"])
            scores[source_id] = scores.get(source_id, 0.0) + 1.0 / (60 + rank)
        ordered = sorted(scores, key=lambda value: (-scores[value], value))[:limit]
        return {source_id: rank for rank, source_id in enumerate(ordered, 1)}

    def _fuse(
        self,
        ranked_lists: list[tuple[str, list[SearchHit]]],
        limit: int,
        wiki_source_ranks: dict[str, int] | None = None,
    ) -> list[SearchHit]:
        fused: dict[tuple[str, str], dict[str, Any]] = {}
        for channel, hits in ranked_lists:
            for rank, hit in enumerate(hits, 1):
                key = (hit.source_id, hit.chunk_id)
                record = fused.setdefault(
                    key,
                    {"hit": hit, "score": 0.0, "channels": [], "breakdown": {}},
                )
                record["score"] += 1.0 / (60 + rank)
                record["channels"].append(channel)
                record["breakdown"].update(hit.score_breakdown)
                if hit.matched_asset_path:
                    record["matched_asset_id"] = hit.matched_asset_id
                    record["matched_asset_path"] = hit.matched_asset_path
        for record in fused.values():
            navigation_rank = (wiki_source_ranks or {}).get(
                record["hit"].source_id
            )
            if navigation_rank:
                record["channels"].append("wiki_navigation")
                record["breakdown"]["wiki_navigation_rank"] = float(
                    navigation_rank
                )
        ordered = sorted(
            fused.values(),
            key=lambda value: (
                -value["score"],
                (wiki_source_ranks or {}).get(value["hit"].source_id, 10**9),
                value["hit"].path,
                value["hit"].chunk_id,
            ),
        )[:limit]
        return [
            SearchHit(
                **{
                    **record["hit"].to_dict(),
                    "score": round(record["score"] * 1000, 6),
                    "retrieval_channels": list(dict.fromkeys(record["channels"])),
                    "score_breakdown": record["breakdown"],
                    "matched_asset_id": record.get("matched_asset_id", ""),
                    "matched_asset_path": record.get("matched_asset_path", ""),
                }
            )
            for record in ordered
        ]

    def _rerank(
        self,
        query: str,
        candidates: list[SearchHit],
        top_k: int,
        provider: BailianRetrievalProvider,
        multimodal: bool,
    ) -> tuple[list[SearchHit], dict[str, int]]:
        if not candidates:
            return [], {}
        documents: list[Any] = []
        for hit in candidates:
            source, chunk = self.chunk_lookup.get(
                (hit.source_id, hit.chunk_id), ({}, {})
            )
            preferred_asset = hit.matched_asset_path or (
                hit.asset_paths[0] if hit.asset_paths else ""
            )
            if multimodal and preferred_asset:
                target = (self.vault / preferred_asset).resolve()
                if self.vault in target.parents and target.is_file():
                    documents.append({"image": image_data_url(target)})
                    continue
            document_text = _document_text(source, chunk)
            documents.append({"text": document_text} if multimodal else document_text)
        if multimodal:
            results, usage = provider.multimodal_rerank(query, documents, top_k)
            channel = "multimodal_rerank"
        else:
            results, usage = provider.text_rerank(
                query, [str(value) for value in documents], top_k
            )
            channel = "text_rerank"
        output: list[SearchHit] = []
        for result in results:
            index = int(result.get("index", -1))
            if index < 0 or index >= len(candidates):
                continue
            hit = candidates[index]
            relevance = float(result.get("relevance_score", 0.0))
            output.append(
                SearchHit(
                    **{
                        **hit.to_dict(),
                        "score": round(relevance, 6),
                        "retrieval_channels": list(
                            dict.fromkeys([*hit.retrieval_channels, channel])
                        ),
                        "score_breakdown": {
                            **hit.score_breakdown,
                            channel: round(relevance, 6),
                        },
                    }
                )
            )
        return output, usage

    def search(
        self,
        query: str,
        top_k: int,
        source_ids: set[str] | None,
        wiki_paths_by_source: dict[str, list[str]],
        wiki_source_ranks: dict[str, int] | None,
        mode: str,
        provider: BailianRetrievalProvider,
    ) -> tuple[list[SearchHit], dict[str, Any]]:
        if mode not in RETRIEVAL_MODES:
            raise ValueError(f"retrieval_mode 必须是 {', '.join(RETRIEVAL_MODES)}")
        candidate_limit = max(20, min(top_k * 4, 40))
        lexical = Retriever(self.state).search(
            query,
            candidate_limit,
            source_ids,
            wiki_paths_by_source,
            wiki_source_ranks,
        )
        trace: dict[str, Any] = {
            "requested_mode": mode,
            "mode": "lexical",
            "channels": (["wiki_navigation"] if wiki_source_ranks else [])
            + ["bm25"],
            "models": {},
            "fallback_reason": None,
            "usage": {},
        }
        if mode == "lexical":
            return lexical[:top_k], trace

        status = self.index.status(self.state, provider)
        if not status["text_ready"]:
            trace["fallback_reason"] = "文本向量索引缺失或已过期"
            return lexical[:top_k], trace
        value = self.index.load()
        try:
            query_vectors, embedding_usage = provider.text_embeddings([query])
            wiki_index = value.get("wiki") if isinstance(value.get("wiki"), dict) else {}
            semantic_page_navigation = self._rank_wiki_pages(
                query_vectors[0], wiki_index.get("records", []), source_ids
            )
            semantic_navigation = (
                self._wiki_pages_to_sources(semantic_page_navigation)
                if semantic_page_navigation
                else self._rank_wiki_sources(
                    query_vectors[0], value["text"]["records"], source_ids
                )
            )
            effective_wiki_source_ranks = self._merge_wiki_source_ranks(
                wiki_source_ranks, semantic_navigation
            )
            trace["wiki_semantic_navigation"] = semantic_navigation
            trace["wiki_semantic_navigation_pages"] = semantic_page_navigation
            trace["wiki_navigation_strategy"] = (
                "wiki-page-bm25+page-embedding->evidence"
                if semantic_page_navigation
                else "wiki-page-bm25+chunk-aggregate-fallback->evidence"
            )
            trace["wiki_navigation_sources"] = [
                {
                    "source_id": source_id,
                    "rank": rank,
                    "title": str(
                        self.state.get("sources", {})
                        .get(source_id, {})
                        .get("title", source_id)
                    ),
                }
                for source_id, rank in effective_wiki_source_ranks.items()
            ]
            semantic = self._rank_vectors(
                query_vectors[0],
                value["text"]["records"],
                query,
                source_ids,
                wiki_paths_by_source,
                candidate_limit,
                "text_embedding",
            )
            ranked_lists: list[tuple[str, list[SearchHit]]] = [
                ("bm25", lexical),
                ("text_embedding", semantic),
            ]
            trace["mode"] = "hybrid"
            trace["channels"] = ["bm25", "text_embedding", "rrf"]
            trace["models"]["text_embedding"] = provider.text_embedding_model
            trace["usage"]["text_embedding"] = embedding_usage

            use_multimodal = mode == "multimodal" and status["visual_ready"]
            if mode == "multimodal" and not use_multimodal:
                trace["fallback_reason"] = "多模态向量索引缺失或已过期，已使用混合检索"
            if use_multimodal:
                visual_query, visual_usage = provider.multimodal_embedding(
                    [{"text": query}], fused=False
                )
                visual = self._rank_vectors(
                    visual_query,
                    value["visual"]["records"],
                    query,
                    source_ids,
                    wiki_paths_by_source,
                    candidate_limit,
                    "multimodal_embedding",
                )
                ranked_lists.append(("multimodal_embedding", visual))
                trace["mode"] = "multimodal"
                trace["channels"].append("multimodal_embedding")
                trace["models"]["multimodal_embedding"] = provider.vl_embedding_model
                trace["usage"]["multimodal_embedding"] = visual_usage

            if effective_wiki_source_ranks:
                trace["channels"].append("wiki_navigation")
            fused = self._fuse(
                ranked_lists, candidate_limit, effective_wiki_source_ranks
            )
            reranked, rerank_usage = self._rerank(
                query, fused, top_k, provider, use_multimodal
            )
            rerank_key = "multimodal_rerank" if use_multimodal else "text_rerank"
            trace["channels"].extend(["rrf", rerank_key])
            trace["channels"] = list(dict.fromkeys(trace["channels"]))
            trace["models"][rerank_key] = (
                provider.vl_rerank_model if use_multimodal else provider.text_rerank_model
            )
            trace["usage"][rerank_key] = rerank_usage
            return (reranked or fused[:top_k]), trace
        except ProviderError as exc:
            trace["fallback_reason"] = str(exc)
            return lexical[:top_k], trace
