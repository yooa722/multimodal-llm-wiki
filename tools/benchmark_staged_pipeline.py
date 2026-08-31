from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.pipeline import WikiPipeline
from mmwiki.provider import OpenAICompatibleProvider, read_dotenv
from mmwiki.retrieval import BailianRetrievalProvider, RetrievalIndex
from tools.evaluate_retrieval import evaluate_pipeline, load_jsonl


DEFAULT_SOURCE_IDS = (
    "104页-ERP财务供应链解决方案",
    "110页-供应链解决方案",
    "20230507-小红书-服饰潮流行业-好产品赢战618",
    "2023年618厨卫刚需品类市场总结-烟-灶-热--12页",
    "23年开年冰洗小结及五一-618预测-8页",
    "3-类典型株型草本植物对沙面风蚀抑制作用的研究",
    "618新生活购物趋势洞察报告-37页",
    "厚叶卷瓣兰_中国兰科一新记录种",
    "服饰潮流行业-闭环全攻略",
    "果集数据-抖音618好物节电商报告-62页",
)


class LocalDeterministicRetrievalProvider:
    """No-network provider for engineering-cost and reuse regression checks."""

    text_embedding_model = "local-deterministic-text-v1"
    text_rerank_model = "not-used-offline"
    vl_embedding_model = "local-deterministic-visual-v1"
    vl_rerank_model = "not-used-offline"
    text_configured = True
    multimodal_configured = True

    @staticmethod
    def _vector(value: str, dimensions: int = 32) -> list[float]:
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return [round(byte / 255, 6) for byte in digest[:dimensions]]

    def text_embeddings(self, texts: list[str]) -> tuple[list[list[float]], dict[str, int]]:
        return [self._vector(text) for text in texts], {"records": len(texts)}

    def multimodal_embedding(
        self, contents: list[dict[str, str]], fused: bool
    ) -> tuple[list[float], dict[str, int]]:
        identity = "|".join(
            f"{key}:{len(value)}:{value[:80]}"
            for content in contents
            for key, value in sorted(content.items())
        )
        return self._vector(identity), {"records": 1}


def directory_bytes(path: Path) -> int:
    return sum(target.stat().st_size for target in path.rglob("*") if target.is_file())


def merge_numeric(values: list[Any]) -> dict[str, int]:
    totals: dict[str, int] = {}
    pending = list(values)
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, bool):
                    continue
                if isinstance(item, (int, float)):
                    totals[str(key)] = totals.get(str(key), 0) + int(item)
                elif isinstance(item, (list, dict)):
                    pending.append(item)
    return totals


def summarize_build(results: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [result.get("build_metrics", {}) for result in results]
    return {
        "sources": len(results),
        "statuses": {
            status: sum(result.get("status") == status for result in results)
            for status in sorted({str(result.get("status") or "") for result in results})
        },
        "elapsed_ms": round(
            sum(float(metric.get("elapsed_ms") or 0) for metric in metrics), 3
        ),
        "api_calls": sum(int(metric.get("api_calls") or 0) for metric in metrics),
        "token_usage": merge_numeric(
            [metric.get("token_usage", {}) for metric in metrics]
        ),
        "created_pages": sum(
            int(metric.get("created_pages") or 0) for metric in metrics
        ),
        "updated_pages": sum(
            int(metric.get("updated_pages") or 0) for metric in metrics
        ),
        "multimodal_items_added": sum(
            int(metric.get("multimodal_items_added") or 0) for metric in metrics
        ),
        "per_source": results,
    }


def corpus_snapshot(pipeline: WikiPipeline) -> dict[str, Any]:
    state = pipeline._load_state()
    sources = list(state.get("sources", {}).values())
    return {
        "sources": len(sources),
        "pages": len(state.get("pages", {})),
        "items": sum(len(source.get("items", [])) for source in sources),
        "chunks": sum(len(source.get("chunks", [])) for source in sources),
        "assets": sum(len(source.get("assets", {})) for source in sources),
        "vault_bytes": directory_bytes(pipeline.vault),
        "index_bytes": (
            pipeline.retrieval_index_path.stat().st_size
            if pipeline.retrieval_index_path.is_file()
            else 0
        ),
        "representations": {
            source_id: source.get("representation")
            for source_id, source in state.get("sources", {}).items()
        },
    }


def timed_index(
    pipeline: WikiPipeline,
    *,
    include_visual: bool,
    source_ids: set[str] | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = (
        RetrievalIndex(pipeline.retrieval_index_path, pipeline.vault).build(
            pipeline._load_state(), provider, include_visual, source_ids
        )
        if provider is not None
        else pipeline.build_retrieval_index(include_visual, source_ids)
    )
    return {**result, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}


def metric_cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value if value is not None else "—")


def markdown_report(payload: dict[str, Any]) -> str:
    text = payload["text_baseline"]
    enhanced = payload["multimodal_enhancement"]
    text_metrics = text["quality"]["metrics"]
    enhanced_metrics = enhanced["quality_multimodal"]["metrics"]
    text_mode = text["quality"]["retrieval_mode"]
    enhanced_mode = enhanced["quality_multimodal"]["retrieval_mode"]
    execution_note = (
        "离线工程回归：模型与向量由确定性本地替身执行，只用于验证增量成本和复用；质量指标不可替代在线模型评测。"
        if payload.get("execution_mode") == "offline-engineering"
        else "在线同源对照：构建、向量和检索调用项目配置的实际模型。"
    )
    return f"""# 文本 LLM Wiki → 多模态增量增强实测

> 生成时间：{payload['generated_at']}<br>
> 对比口径：同一 Source Package、同一来源版本、同一评测集；基线仅使用文本代理，多模态阶段增加完整表格、公式、原图、视觉向量与视觉重排。
> 执行口径：{execution_note}

## 结论先行

- 文本基座包含 {text['snapshot']['sources']} 个来源、{text['snapshot']['chunks']} 个文本 Chunk；多模态阶段新增 {enhanced['build']['multimodal_items_added']} 个一等多模态 Item 和 {enhanced['snapshot']['assets']} 个视觉资源。
- 增量索引复用了 {enhanced['index']['reused_text_records']} 条文本向量，新建 {enhanced['index']['new_text_records']} 条文本向量和 {enhanced['index']['new_visual_records']} 条视觉向量。
- Recall@5 从 {metric_cell(text_metrics['recall_at_k'])} 变为 {metric_cell(enhanced_metrics['recall_at_k'])}，MRR 从 {metric_cell(text_metrics['mrr'])} 变为 {metric_cell(enhanced_metrics['mrr'])}；请求模式回退数分别为 {text_metrics['fallback_count']} 和 {enhanced_metrics['fallback_count']}。
- 相同版本重复执行多模态阶段，模型调用为 {enhanced['idempotent_repeat']['api_calls']}，说明重复摄入不会重复付费构建。

## 构建与索引成本

| 指标 | 文本 Wiki 基线 | 多模态增量 | 说明 |
|---|---:|---:|---|
| 构建耗时（ms） | {metric_cell(text['build']['elapsed_ms'])} | {metric_cell(enhanced['build']['elapsed_ms'])} | 各来源阶段耗时之和 |
| 构建模型调用 | {text['build']['api_calls']} | {enhanced['build']['api_calls']} | 不含检索评测调用 |
| 创建 / 更新页面 | {text['build']['created_pages']} / {text['build']['updated_pages']} | {enhanced['build']['created_pages']} / {enhanced['build']['updated_pages']} | 增量阶段只触碰相关页面 |
| 索引耗时（ms） | {metric_cell(text['index']['elapsed_ms'])} | {metric_cell(enhanced['index']['elapsed_ms'])} | 多模态列为增量更新 |
| 复用 / 新建文本向量 | {text['index']['reused_text_records']} / {text['index']['new_text_records']} | {enhanced['index']['reused_text_records']} / {enhanced['index']['new_text_records']} | 复用率可直接反映增量效率 |
| 新建视觉向量 | {text['index']['new_visual_records']} | {enhanced['index']['new_visual_records']} | 文本基线不建视觉向量 |
| Vault 大小（bytes） | {text['snapshot']['vault_bytes']} | {enhanced['snapshot']['vault_bytes']} | 页面与原始 Evidence 展示副本 |
| 索引大小（bytes） | {text['snapshot']['index_bytes']} | {enhanced['snapshot']['index_bytes']} | 文本向量 + 视觉向量 |

## 检索效果

| 指标 | 文本基线（{text_mode}） | 多模态增强（{enhanced_mode}） |
|---|---:|---:|
| Recall@5 | {metric_cell(text_metrics['recall_at_k'])} | {metric_cell(enhanced_metrics['recall_at_k'])} |
| MRR | {metric_cell(text_metrics['mrr'])} | {metric_cell(enhanced_metrics['mrr'])} |
| Top-1 | {metric_cell(text_metrics['top1_accuracy'])} | {metric_cell(enhanced_metrics['top1_accuracy'])} |
| nDCG@5 | {metric_cell(text_metrics['ndcg_at_k'])} | {metric_cell(enhanced_metrics['ndcg_at_k'])} |
| Wiki 来源 Recall@3 | {metric_cell(text_metrics['wiki_source_recall_at_k'])} | {metric_cell(enhanced_metrics['wiki_source_recall_at_k'])} |
| 平均延迟（ms） | {metric_cell(text_metrics['latency_ms_mean'])} | {metric_cell(enhanced_metrics['latency_ms_mean'])} |
| P95 延迟（ms） | {metric_cell(text_metrics['latency_ms_p95'])} | {metric_cell(enhanced_metrics['latency_ms_p95'])} |
| 回退数 | {text_metrics['fallback_count']} | {enhanced_metrics['fallback_count']} |

详细逐题结果与按模态分组指标见同名 JSON 文件。
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在隔离工作区实测文本 LLM Wiki 与多模态增量阶段"
    )
    parser.add_argument(
        "--package",
        type=Path,
        action="append",
        default=[],
        help="Source Package，可重复；默认使用当前 data/index.json 中的 10 个来源",
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=PROJECT_ROOT / "evaluation/official_image_text_10_verified.jsonl",
    )
    parser.add_argument(
        "--provider", choices=("baseline", "api"), default="api"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "reports/staged-pipeline-benchmark.json",
    )
    parser.add_argument(
        "--full-scale-source",
        action="append",
        default=[],
        help="在指定 package_id 的多模态阶段启用逐页全量分析",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="不外发数据：使用本地确定性构建/向量和 lexical 评测，仅验证工程增量成本",
    )
    args = parser.parse_args()
    if args.offline and args.provider == "api":
        args.provider = "baseline"

    dataset_index = json.loads(
        (PROJECT_ROOT / "data/index.json").read_text(encoding="utf-8")
    )
    package_paths = {
        str(record["package_id"]): PROJECT_ROOT / "data" / str(record["path"])
        for record in dataset_index["packages"]
    }
    packages = args.package or [package_paths[source_id] for source_id in DEFAULT_SOURCE_IDS]
    packages = [path.expanduser().resolve() for path in packages]
    missing = [str(path) for path in packages if not (path / "manifest.json").is_file()]
    if missing:
        raise SystemExit("缺少 Source Package：" + ", ".join(missing))

    package_ids = {
        str(json.loads((path / "manifest.json").read_text(encoding="utf-8"))["package_id"])
        for path in packages
    }
    cases = [
        case for case in load_jsonl(args.suite) if case["source_id"] in package_ids
    ]
    if not cases:
        raise SystemExit("评测集没有覆盖选中的 Source Package")

    configured = read_dotenv(PROJECT_ROOT / ".env")
    previous_environment = {key: os.environ.get(key) for key in configured}
    for key, value in configured.items():
        os.environ.setdefault(key, value)

    try:
        with tempfile.TemporaryDirectory(prefix="mmwiki-staged-benchmark-") as directory:
            benchmark_root = Path(directory)
            (benchmark_root / "config").mkdir(parents=True)
            shutil.copy2(PROJECT_ROOT / "config/schema.md", benchmark_root / "config/schema.md")
            shutil.copy2(PROJECT_ROOT / "config/purpose.md", benchmark_root / "config/purpose.md")
            pipeline = WikiPipeline(benchmark_root)
            local_retrieval_provider = (
                LocalDeterministicRetrievalProvider() if args.offline else None
            )

            text_results = [
                pipeline.ingest(path, provider=args.provider, stage="text")
                for path in packages
            ]
            text_index = timed_index(
                pipeline,
                include_visual=False,
                provider=local_retrieval_provider,
            )
            text_quality = evaluate_pipeline(
                pipeline,
                cases,
                retrieval_mode="lexical" if args.offline else "hybrid",
                scope="corpus",
            )
            text_payload = {
                "build": summarize_build(text_results),
                "index": text_index,
                "snapshot": corpus_snapshot(pipeline),
                "quality": text_quality,
            }

            multimodal_results = [
                pipeline.ingest(
                    path,
                    provider=args.provider,
                    stage="multimodal",
                    full_scale=(
                        json.loads((path / "manifest.json").read_text(encoding="utf-8"))[
                            "package_id"
                        ]
                        in set(args.full_scale_source)
                    ),
                )
                for path in packages
            ]
            multimodal_index = timed_index(
                pipeline,
                include_visual=True,
                source_ids=package_ids,
                provider=local_retrieval_provider,
            )
            enhanced_hybrid_quality = evaluate_pipeline(
                pipeline,
                cases,
                retrieval_mode="lexical" if args.offline else "hybrid",
                scope="corpus",
            )
            multimodal_quality = evaluate_pipeline(
                pipeline,
                cases,
                retrieval_mode="lexical" if args.offline else "multimodal",
                scope="corpus",
            )
            repeated = [
                pipeline.ingest(
                    path,
                    provider=args.provider,
                    stage="multimodal",
                    full_scale=(
                        json.loads((path / "manifest.json").read_text(encoding="utf-8"))[
                            "package_id"
                        ]
                        in set(args.full_scale_source)
                    ),
                )
                for path in packages
            ]

            build_provider = OpenAICompatibleProvider(benchmark_root, "build")
            vision_provider = OpenAICompatibleProvider(benchmark_root, "vision")
            retrieval_provider = BailianRetrievalProvider(benchmark_root)
            payload = {
                "schema_version": "mmwiki-staged-benchmark-0.1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "execution_mode": (
                    "offline-engineering" if args.offline else "online-model"
                ),
                "provider": args.provider,
                "baseline_definition": (
                    "Karpathy-style text LLM Wiki operational baseline: same immutable "
                    "Source Packages and evaluation set, using text/OCR/caption/linearized "
                    "proxies without structured tables, image pixels or visual retrieval."
                ),
                "packages": sorted(package_ids),
                "evaluation_cases_total": len(cases),
                "models": (
                    {
                        "wiki_builder": "deterministic-baseline",
                        "vision_analysis_and_qa": "not-used-offline",
                        "text_embedding": local_retrieval_provider.text_embedding_model,
                        "text_rerank": local_retrieval_provider.text_rerank_model,
                        "visual_embedding": local_retrieval_provider.vl_embedding_model,
                        "visual_rerank": local_retrieval_provider.vl_rerank_model,
                    }
                    if args.offline
                    else {
                        "wiki_builder": build_provider.model,
                        "vision_analysis_and_qa": vision_provider.model,
                        "text_embedding": retrieval_provider.text_embedding_model,
                        "text_rerank": retrieval_provider.text_rerank_model,
                        "visual_embedding": retrieval_provider.vl_embedding_model,
                        "visual_rerank": retrieval_provider.vl_rerank_model,
                    }
                ),
                "text_baseline": text_payload,
                "multimodal_enhancement": {
                    "build": summarize_build(multimodal_results),
                    "index": multimodal_index,
                    "snapshot": corpus_snapshot(pipeline),
                    "quality_hybrid": enhanced_hybrid_quality,
                    "quality_multimodal": multimodal_quality,
                    "idempotent_repeat": summarize_build(repeated),
                },
            }
    finally:
        for key, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(markdown_report(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "completed",
                "json": str(args.output),
                "markdown": str(markdown_path),
                "text_metrics": payload["text_baseline"]["quality"]["metrics"],
                "multimodal_metrics": payload["multimodal_enhancement"][
                    "quality_multimodal"
                ]["metrics"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
