from __future__ import annotations

import re
import math
from collections import Counter
from pathlib import Path
from typing import Any

from .models import SearchHit
from .visual_evidence import iter_retrieval_chunks


def token_list(text: str) -> list[str]:
    lowered = text.casefold()
    latin = re.findall(r"[a-z0-9]+(?:[._/@+-][a-z0-9]+)*", lowered)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.extend(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    return latin + chinese


def tokens(text: str) -> set[str]:
    return set(token_list(text))


def reference_labels(text: str) -> set[str]:
    return set(
        re.findall(
            r"\b(?:figure|fig\.?|table|equation|eq\.?)\s*[a-z0-9.-]+|(?:图|表|公式)\s*[a-z0-9.-]+",
            text.casefold(),
        )
    )


def excerpt(text: str, query: str, limit: int = 500) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    positions = [clean.casefold().find(token) for token in tokens(query) if token]
    positions = [value for value in positions if value >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    end = min(len(clean), start + limit)
    return ("…" if start else "") + clean[start:end] + ("…" if end < len(clean) else "")


def make_search_hit(
    source_id: str,
    source: dict[str, Any],
    chunk: dict[str, Any],
    query: str,
    score: float,
    wiki_paths: list[str],
    channels: list[str] | None = None,
    score_breakdown: dict[str, float] | None = None,
    matched_asset_id: str = "",
    matched_asset_path: str = "",
) -> SearchHit:
    asset_paths = [
        source.get("assets", {}).get(asset_id, {}).get("vault_path", "")
        for asset_id in chunk.get("asset_ids", [])
    ]
    return SearchHit(
        source_id=source_id,
        chunk_id=str(chunk["chunk_id"]),
        title=str(chunk.get("breadcrumb") or source["title"]),
        score=score,
        snippet=excerpt(str(chunk.get("text") or ""), query),
        item_ids=[str(value) for value in chunk.get("item_ids", [])],
        modalities=[str(value) for value in chunk.get("modalities", [])],
        asset_paths=[value for value in asset_paths if value],
        pages=[int(value) for value in chunk.get("page_refs", [])],
        path=str(source["wiki_path"]),
        wiki_paths=wiki_paths,
        retrieval_channels=channels or [],
        score_breakdown=score_breakdown or {},
        matched_asset_id=matched_asset_id,
        matched_asset_path=matched_asset_path,
    )


def navigate_wiki(
    state: dict[str, Any],
    vault: Path,
    query: str,
    source_ids: set[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank real Wiki pages before Evidence retrieval.

    Wiki pages are the navigation substrate, so this deliberately ranks page
    titles, summaries and bodies as documents instead of treating them as a
    tiny bonus on top of chunk retrieval.  Link authority is only a tie-breaker:
    a popular page must still match the user's question.
    """
    query_token_list = token_list(query)
    query_tokens = set(query_token_list)
    query_labels = reference_labels(query)
    pages: list[dict[str, Any]] = []
    for source_id, source in state.get("sources", {}).items():
        if source_ids is not None and source_id not in source_ids:
            continue
        pages.append(
            {
                "path": str(source.get("wiki_path") or ""),
                "title": str(source.get("title") or source_id),
                "kind": "source",
                "summary": "",
                "source_ids": [str(source_id)],
            }
        )
        evidence_map_path = str(source.get("evidence_map_path") or "")
        if evidence_map_path:
            pages.append(
                {
                    "path": evidence_map_path,
                    "title": f"{source.get('title') or source_id} · 多模态证据地图",
                    "kind": "evidence-map",
                    "summary": "按页码和结构汇总图片、图表、表格与公式 Evidence。",
                    "source_ids": [str(source_id)],
                }
            )
    for page in state.get("pages", {}).values():
        page_source_ids = [str(value) for value in page.get("source_ids", [])]
        if source_ids is not None and not source_ids.intersection(page_source_ids):
            continue
        pages.append(
            {
                "path": str(page.get("path") or ""),
                "title": str(page.get("title") or ""),
                "kind": str(page.get("kind") or "analysis"),
                "summary": str(page.get("summary") or ""),
                "source_ids": page_source_ids,
            }
        )

    documents: list[dict[str, Any]] = []
    vault_root = vault.resolve()
    for page in pages:
        relative = page["path"]
        target = (vault_root / relative).resolve()
        if not relative or vault_root not in target.parents or not target.is_file():
            continue
        content = target.read_text(encoding="utf-8")[:100000]
        title_tokens = token_list(page["title"])
        summary_tokens = token_list(page["summary"])
        body_tokens = token_list(content)
        # Field weighting preserves the familiar Wiki behaviour: page titles
        # and concise summaries dominate long generated bodies.
        weighted_tokens = title_tokens * 4 + summary_tokens * 2 + body_tokens
        documents.append(
            {
                **page,
                "content": content,
                "tokens": weighted_tokens,
                "token_set": set(weighted_tokens),
            }
        )

    if not documents or not query_tokens:
        return []

    aliases: dict[str, str] = {}
    for page in documents:
        path = page["path"].removesuffix(".md")
        for alias in (path, Path(path).name, page["title"]):
            aliases.setdefault(str(alias).casefold(), page["path"])
    inbound = Counter({page["path"]: 0 for page in documents})
    link_pattern = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
    for page in documents:
        linked: set[str] = set()
        for raw_link in link_pattern.findall(page["content"]):
            resolved = aliases.get(raw_link.strip().removesuffix(".md").casefold())
            if resolved and resolved != page["path"]:
                linked.add(resolved)
        inbound.update(linked)

    document_count = len(documents)
    average_length = sum(len(page["tokens"]) for page in documents) / document_count
    document_frequency = {
        token: sum(token in page["token_set"] for page in documents)
        for token in query_tokens
    }
    ranked: list[dict[str, Any]] = []
    for page in documents:
        counts = Counter(page["tokens"])
        lexical_score = 0.0
        for token in query_tokens:
            frequency = counts[token]
            if not frequency:
                continue
            frequency_in_docs = document_frequency[token]
            inverse_document_frequency = math.log(
                1
                + (document_count - frequency_in_docs + 0.5)
                / (frequency_in_docs + 0.5)
            )
            normalization = frequency + 1.2 * (
                1 - 0.75 + 0.75 * len(page["tokens"]) / max(average_length, 1.0)
            )
            lexical_score += inverse_document_frequency * (
                frequency * (1.2 + 1) / normalization
            )
        label_score = (
            6.0
            if query_labels
            & reference_labels("\n".join([page["title"], page["content"]]))
            else 0.0
        )
        authority_score = math.log1p(inbound[page["path"]]) * 0.15
        score = lexical_score + label_score + authority_score
        if lexical_score + label_score <= 0:
            continue
        ranked.append(
            {
                **{key: value for key, value in page.items() if key not in {"content", "tokens", "token_set"}},
                "score": round(score, 6),
                "navigation_stage": "wiki-page-bm25",
                "matched_terms": sorted(query_tokens & page["token_set"]),
                "inbound_links": int(inbound[page["path"]]),
                "score_breakdown": {
                    "page_bm25": round(lexical_score, 6),
                    "reference_label": round(label_score, 6),
                    "link_authority": round(authority_score, 6),
                },
            }
        )
    ranked.sort(
        key=lambda value: (
            -value["score"],
            0 if value["kind"] != "source" else 1,
            value["path"],
        )
    )
    return ranked[: max(1, min(limit, 20))]


class Retriever:
    def __init__(self, state: dict[str, Any]):
        self.state = state

    def search(
        self,
        query: str,
        top_k: int,
        source_ids: set[str] | None,
        wiki_paths_by_source: dict[str, list[str]] | None = None,
        wiki_source_ranks: dict[str, int] | None = None,
    ) -> list[SearchHit]:
        query_tokens = tokens(query)
        query_labels = reference_labels(query)
        visual_intent = any(
            word in query.casefold() for word in ("图", "图片", "图表", "figure", "chart", "image")
        )
        table_intent = any(
            word in query.casefold() for word in ("表", "表格", "table", "行", "列")
        )
        candidates: list[
            tuple[str, dict[str, Any], dict[str, Any], str, list[str], set[str]]
        ] = []
        for package_id, source in self.state.get("sources", {}).items():
            if source_ids is not None and package_id not in source_ids:
                continue
            for chunk in iter_retrieval_chunks(source):
                searchable = "\n".join(
                    [str(chunk.get("breadcrumb") or ""), str(chunk.get("text") or "")]
                )
                document_tokens = token_list(searchable)
                candidates.append(
                    (
                        str(package_id),
                        source,
                        chunk,
                        searchable,
                        document_tokens,
                        set(document_tokens),
                    )
                )

        document_count = len(candidates)
        average_length = (
            sum(len(value[4]) for value in candidates) / document_count
            if document_count
            else 1.0
        )
        document_frequency = {
            token: sum(token in value[5] for value in candidates)
            for token in query_tokens
        }
        hits: list[SearchHit] = []
        for package_id, source, chunk, searchable, document_tokens, _ in candidates:
            counts = Counter(document_tokens)
            term_counts = {
                token: counts[token] for token in query_tokens if counts[token]
            }
            score = 0.0
            for token, frequency in term_counts.items():
                frequency_in_docs = document_frequency[token]
                inverse_document_frequency = math.log(
                    1
                    + (document_count - frequency_in_docs + 0.5)
                    / (frequency_in_docs + 0.5)
                )
                normalization = frequency + 1.2 * (
                    1 - 0.75 + 0.75 * len(document_tokens) / average_length
                )
                score += inverse_document_frequency * (
                    frequency * (1.2 + 1) / normalization
                )
            if query_labels & reference_labels(searchable):
                score += 6
            modalities = [str(value) for value in chunk.get("modalities", [])]
            if visual_intent and set(modalities) & {
                "image",
                "chart",
                "figure",
                "image_caption",
                "image_ocr",
            }:
                score += 4
            if table_intent and "table" in modalities:
                score += 4
            navigation_rank = (wiki_source_ranks or {}).get(str(package_id))
            navigation_score = (
                round(0.001 / navigation_rank, 6) if navigation_rank else 0.0
            )
            if navigation_score:
                score += navigation_score
            if score <= 0:
                continue
            channels = ["bm25"]
            breakdown = {"bm25": round(score - navigation_score, 6)}
            if navigation_score:
                channels.insert(0, "wiki_navigation")
                breakdown["wiki_navigation"] = navigation_score
            hits.append(
                make_search_hit(
                    str(package_id),
                    source,
                    chunk,
                    query,
                    score,
                    (wiki_paths_by_source or {}).get(package_id, []),
                    channels,
                    breakdown,
                )
            )
        hits.sort(key=lambda value: (-value.score, value.path, value.chunk_id))
        return hits[: max(1, min(top_k, 20))]
