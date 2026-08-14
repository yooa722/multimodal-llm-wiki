from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
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


RETRIEVAL_INDEX_VERSION = "mmwiki-retrieval-0.1"
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
        fresh = (
            value.get("schema_version") == RETRIEVAL_INDEX_VERSION
            and value.get("sources") == current_sources
        )
        text = value.get("text") if isinstance(value.get("text"), dict) else {}
        visual = value.get("visual") if isinstance(value.get("visual"), dict) else {}
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
            "text_model": text.get("model") or provider.text_embedding_model,
            "text_rerank_model": provider.text_rerank_model,
            "visual_model": visual.get("model") or provider.vl_embedding_model,
            "visual_rerank_model": provider.vl_rerank_model,
            "text_records": len(text.get("records", [])),
            "visual_records": len(visual.get("records", [])),
            "created_at": value.get("created_at"),
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
        existing_text_records: list[dict[str, Any]] = []
        existing_visual_records: list[dict[str, Any]] = []
        if incremental:
            current_versions = _source_versions(state)
            existing_versions = existing.get("sources", {})
            if existing.get("schema_version") != RETRIEVAL_INDEX_VERSION:
                raise ProviderError("现有索引版本不兼容，无法安全增量合并")
            stale = [
                source_id
                for source_id in sorted(retained_source_ids)
                if not isinstance(existing_versions, dict)
                or existing_versions.get(source_id) != current_versions.get(source_id)
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
                and str(record.get("source_id") or "") in retained_source_ids
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
            }
            if indexed_retained_chunks != expected_retained_chunks:
                raise ProviderError("未选来源的文本索引记录不完整，拒绝标记为最新")

            if include_visual:
                existing_visual = (
                    existing.get("visual")
                    if isinstance(existing.get("visual"), dict)
                    else {}
                )
                if existing_visual.get("model") != provider.vl_embedding_model:
                    raise ProviderError("现有视觉索引模型不一致，无法安全增量合并")
                existing_visual_records = [
                    record
                    for record in existing_visual.get("records", [])
                    if isinstance(record, dict)
                    and str(record.get("source_id") or "") in retained_source_ids
                ]

        text_meta: list[dict[str, str]] = []
        texts: list[str] = []
        for source_id, source in sources.items():
            if str(source_id) not in selected_source_ids:
                continue
            for chunk in source.get("chunks", []):
                text_meta.append(
                    {
                        "source_id": str(source_id),
                        "chunk_id": str(chunk.get("chunk_id") or ""),
                    }
                )
                texts.append(_document_text(source, chunk))
        vectors, text_usage = provider.text_embeddings(texts)
        text_records = existing_text_records + [
            {**meta, "vector": vector} for meta, vector in zip(text_meta, vectors)
        ]

        visual_records: list[dict[str, Any]] = list(existing_visual_records)
        visual_usage: dict[str, int] = {}
        if include_visual:
            if not provider.multimodal_configured:
                raise ProviderError("多模态向量与重排接口未配置")
            for source_id, source in sources.items():
                if str(source_id) not in selected_source_ids:
                    continue
                for chunk in source.get("chunks", []):
                    text = _document_text(source, chunk)[:3000]
                    for asset_id in chunk.get("asset_ids", []):
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
                        if not (mime or mimetypes.guess_type(target.name)[0] or "").startswith(
                            "image/"
                        ):
                            continue
                        vector, usage = provider.multimodal_embedding(
                            [
                                {"text": text},
                                {"image": image_data_url(target, mime)},
                            ],
                            fused=True,
                        )
                        _usage_total(visual_usage, usage)
                        visual_records.append(
                            {
                                "source_id": str(source_id),
                                "chunk_id": str(chunk.get("chunk_id") or ""),
                                "asset_id": str(asset_id),
                                "asset_path": vault_path,
                                "vector": vector,
                            }
                        )

        value = {
            "schema_version": RETRIEVAL_INDEX_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sources": _source_versions(state),
            "text": {
                "model": provider.text_embedding_model,
                "records": text_records,
                "usage": text_usage,
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
            "visual_usage": visual_usage,
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
            semantic_navigation = self._rank_wiki_sources(
                query_vectors[0], value["text"]["records"], source_ids
            )
            effective_wiki_source_ranks = self._merge_wiki_source_ranks(
                wiki_source_ranks, semantic_navigation
            )
            trace["wiki_semantic_navigation"] = semantic_navigation
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
