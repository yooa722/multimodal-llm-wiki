from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.pipeline import PipelineError, WikiPipeline
from mmwiki.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    normalize_math_markdown,
)
from mmwiki.web import media_url, wiki_view_url


DEMO_CASES = {
    "table": {
        "question": "开发测试阶段需要多少天、多少人、多少预算？",
        "mode": "auto",
        "why": "验证 Wiki 能否定位知识页，并回读完整表格单元格。",
    },
    "visual": {
        "question": "根据 Figure 4，ReToken 推理时的数据流是什么？请按顺序说明，并指出图中缓存的对象。",
        "mode": "auto",
        "why": "验证系统是否真正读取原图，而不只依赖 Caption。",
    },
    "refuse": {
        "question": "Figure 4 中蓝色方框的 RGB 精确数值是多少？",
        "mode": "auto",
        "why": "验证证据不足时是否拒绝编造。",
    },
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def wiki_link(label: str, relative_path: str) -> str:
    return f"[{label}]({wiki_view_url(relative_path)})"


def evidence_image(label: str, relative_path: str) -> str:
    url = media_url(relative_path)
    return f"[在浏览器中打开原图]({url})\n\n![{label}]({url})"


def api_health() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:19828/api/v1/health", timeout=1
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else {"status": "unknown"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {"status": "offline"}


def collect_snapshot(pipeline: WikiPipeline) -> dict[str, Any]:
    state = pipeline._load_state()
    lint = pipeline.lint()
    retrieval = pipeline.retrieval_status()
    sources = list(state.get("sources", {}).values())
    items = sum(len(source.get("items", [])) for source in sources)
    chunks = sum(len(source.get("chunks", [])) for source in sources)
    assets = sum(len(source.get("assets", [])) for source in sources)
    answer_provider = OpenAICompatibleProvider(
        PROJECT_ROOT,
        "vision" if pipeline.features.enable_vlm else "answer",
    )
    opencode_cli = shutil.which("opencode") or str(
        Path.home() / ".opencode/bin/opencode"
    )
    config = load_json(PROJECT_ROOT / "opencode.json")
    data_ready = lint.get("status") == "passed" and (
        not pipeline.features.enable_vector_retrieval
        or bool(retrieval.get("text_ready"))
    )
    return {
        "data_ready": bool(data_ready),
        "provider_ready": answer_provider.configured,
        "feature_config": pipeline.features.as_dict(),
        "sources": len(sources),
        "pages": len(state.get("pages", {})),
        "items": items,
        "chunks": chunks,
        "assets": assets or retrieval.get("visual_records", 0),
        "lint": lint,
        "retrieval": retrieval,
        "api": api_health(),
        "models": {
            "opencode": config.get("model", "未配置"),
            "wiki_builder": next(
                (source.get("model") for source in sources if source.get("model")),
                "未记录",
            ),
            "vision_qa": answer_provider.model or "未配置",
            "text_embedding": retrieval.get("text_model", "未配置"),
            "text_rerank": retrieval.get("text_rerank_model", "未配置"),
            "visual_embedding": retrieval.get("visual_model", "未配置"),
            "visual_rerank": retrieval.get("visual_rerank_model", "未配置"),
        },
        "opencode_cli": opencode_cli,
        "opencode_cli_ready": Path(opencode_cli).is_file(),
        "opencode_desktop_ready": Path("/Applications/OpenCode.app").is_dir(),
    }


def render_start(pipeline: WikiPipeline) -> str:
    snapshot = collect_snapshot(pipeline)
    status = "可以演示" if snapshot["data_ready"] and snapshot["provider_ready"] else "需要检查"
    return f"""# 多模态 LLM Wiki · OpenCode 新手入口

> **当前结论：{status}。** OpenCode 是操作台，Wiki 页面与多模态 Evidence 才是知识本体。

## 你在 OpenCode 中看到的四部分

| 看到的内容 | 它是什么 | 你需要做什么 |
|---|---|---|
| 左侧项目与会话 | 当前仓库和历史问答 | 保持打开本项目即可 |
| 中间对话区 | 操作 Wiki 的自然语言入口 | 输入下方斜杠命令 |
| Wiki Markdown 页面 | 可浏览、可链接、可维护的知识页 | 点击回答中的页面链接核验 |
| 图片、表格和 Evidence ID | 回到原始事实的证据 | 检查答案是否有依据 |

## 当前 Wiki 快照

- 来源：**{snapshot['sources']}** 个
- 稳定知识页：**{snapshot['pages']}** 个
- 文本 Chunk：**{snapshot['chunks']}** 个
- 视觉资源：**{snapshot['assets']}** 个
- Wiki 页面索引：**{snapshot['retrieval'].get('wiki_records', 0)}** 页（{'可用' if snapshot['retrieval'].get('wiki_semantic_ready') else '待构建'}）
- Evidence 文本/视觉索引：**{snapshot['retrieval'].get('text_records', 0)} / {snapshot['retrieval'].get('visual_records', 0)}**
- Wiki 结构检查：**{snapshot['lint'].get('status', 'unknown')}**

## 你只需要记住五条命令

1. `/wiki-start`：回到本页。
2. `/wiki-demo`：完整看一遍“Wiki → 表格/图片 Evidence”的过程。
3. `/wiki-table`：现场演示完整表格问答。
4. `/wiki-image`：现场演示原图理解问答。
5. `/wiki-ask 你的问题`：自由提问。

第一次使用请直接输入 **`/wiki-demo`**。不要手动运行底层 Python 命令。
"""


def render_status(pipeline: WikiPipeline) -> str:
    snapshot = collect_snapshot(pipeline)
    retrieval = snapshot["retrieval"]
    api = snapshot["api"]
    api_online = api.get("status") not in {"offline", "error", None}
    checks = [
        ("Wiki 数据与页面", snapshot["data_ready"], f"{snapshot['sources']} 来源 / {snapshot['pages']} 知识页"),
        ("Wiki 页面语义索引", retrieval.get("wiki_semantic_ready"), f"{retrieval.get('wiki_records', 0)} 页"),
        (
            "文本检索索引",
            (retrieval.get("text_ready") if pipeline.features.enable_vector_retrieval else True),
            (
                f"{retrieval.get('text_records', 0)} 条"
                if pipeline.features.enable_vector_retrieval
                else "默认关闭，使用 BM25 + Caption"
            ),
        ),
        (
            "视觉检索索引",
            (
                retrieval.get("visual_ready")
                if pipeline.features.enable_vlm and pipeline.features.enable_vector_retrieval
                else True
            ),
            (
                f"{retrieval.get('visual_records', 0)} 条"
                if pipeline.features.enable_vlm and pipeline.features.enable_vector_retrieval
                else "默认关闭，按需手动开启"
            ),
        ),
        ("模型配置", snapshot["provider_ready"], snapshot["models"]["vision_qa"]),
        ("OpenCode CLI", snapshot["opencode_cli_ready"], snapshot["opencode_cli"]),
        ("OpenCode Desktop", snapshot["opencode_desktop_ready"], "/Applications/OpenCode.app"),
        (
            "本地 Wiki 展示服务",
            api_online,
            (
                "在线；Wiki 链接和原图可打开"
                if api_online
                else "当前进程不可见；OpenCode 类型化工具会按需自动启动"
            ),
        ),
    ]
    lines = [
        "# 演示就绪检查",
        "",
        "| 检查项 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'✅' if ok else '⚠️'} | {detail} |")
    optional_checks = {"Wiki 页面语义索引"}
    blocking = [
        name
        for name, ok, _ in checks[:-1]
        if not ok and name not in optional_checks
    ]
    pending_optional = [
        name for name, ok, _ in checks if not ok and name in optional_checks
    ]
    lines.extend(["", "## 结论", ""])
    if blocking:
        lines.append("**暂不建议演示。** 阻塞项：" + "、".join(blocking) + "。")
    else:
        lines.append(
            "**核心查询可以演示。** OpenCode 类型化工具会按需启动本机展示服务；"
            "如果 Wiki 链接或原图仍不可打开，请完全退出并重开 OpenCode Desktop。"
        )
        if pending_optional:
            lines.append(
                "当前由 Page BM25 完成 Wiki-first 页面定位；页面语义索引是可独立回填的增强项，"
                "不影响现有 Evidence 检索与图文问答。"
            )
    vector_enabled = bool(
        snapshot["feature_config"].get("enable_vector_retrieval")
    )
    vlm_enabled = bool(snapshot["feature_config"].get("enable_vlm"))
    if not vector_enabled:
        auto_path = "Page BM25 + Evidence BM25 + MinerU Caption"
    elif vlm_enabled:
        auto_path = "普通问题 Hybrid；视觉问题 Multimodal"
    else:
        auto_path = "Hybrid；视觉问题使用 Caption 与关联原图回读"

    lines.extend(
        [
            "",
            "## 当前模型",
            "",
            "| 环节 | 模型 |",
            "|---|---|",
            f"| OpenCode Agent | `{snapshot['models']['opencode']}` |",
            f"| Wiki 页面构建 | `{snapshot['models']['wiki_builder']}` |",
            f"| 图文问答 | `{snapshot['models']['vision_qa']}` |",
            f"| 文本向量 / 重排 | `{snapshot['models']['text_embedding']}` / `{snapshot['models']['text_rerank']}` |",
            f"| 视觉向量 / 重排 | `{snapshot['models']['visual_embedding']}` / `{snapshot['models']['visual_rerank']}` |",
            "",
            "## 增量多模态开关",
            "",
            f"- VLM：`{'on' if vlm_enabled else 'off'}`",
            f"- 向量检索：`{'on' if vector_enabled else 'off'}`",
            "- Caption 来源：`MinerU（已有 Wiki 导入时）`",
            f"- Auto 查询路径：`{auto_path}`",
        ]
    )
    return "\n".join(lines) + "\n"


def find_item(state: dict[str, Any], source_id: str, item_id: str) -> dict[str, Any]:
    source = state.get("sources", {}).get(source_id, {})
    for item in source.get("items", []):
        if item.get("item_id") == item_id:
            return item
    return {}


def markdown_table(table: dict[str, Any]) -> str:
    rows = table.get("rows", []) if isinstance(table, dict) else []
    if not rows:
        return "_未找到结构化表格行。_"
    normalized = [[str(cell).replace("|", "\\|") for cell in row] for row in rows]
    width = max(len(row) for row in normalized)
    normalized = [row + [""] * (width - len(row)) for row in normalized]
    header = normalized[0]
    body = normalized[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * width) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def top_demo_hit(pipeline: WikiPipeline, question: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result = pipeline.search_with_trace(question, 3, None, "lexical")
    hits = result.get("hits", [])
    return (hits[0] if hits else {}), result.get("retrieval", {})


def render_tour(pipeline: WikiPipeline) -> str:
    snapshot = collect_snapshot(pipeline)
    state = pipeline._load_state()
    table_case = DEMO_CASES["table"]
    visual_case = DEMO_CASES["visual"]
    table_hit, table_trace = top_demo_hit(pipeline, table_case["question"])
    visual_hit, visual_trace = top_demo_hit(pipeline, visual_case["question"])

    table_source = table_hit.get("source_id", "")
    table_item_id = (table_hit.get("item_ids") or [""])[0]
    table_item = find_item(state, table_source, table_item_id)
    table_version = state.get("sources", {}).get(table_source, {}).get("source_version", "")
    table_evidence = f"{table_source}@{table_version}#{table_item_id}"
    table_wiki = (table_trace.get("wiki_navigation") or [{}])[0].get("path", "")
    table_asset = (table_hit.get("asset_paths") or [""])[0]

    visual_source = visual_hit.get("source_id", "")
    visual_item_id = (visual_hit.get("item_ids") or [""])[0]
    visual_version = state.get("sources", {}).get(visual_source, {}).get("source_version", "")
    visual_evidence = f"{visual_source}@{visual_version}#{visual_item_id}"
    visual_wiki = (visual_trace.get("wiki_navigation") or [{}])[0].get("path", "")
    visual_asset = (visual_hit.get("asset_paths") or [""])[0]

    return f"""# 多模态 LLM Wiki 完整导览

## 1. 一句话看懂

**OpenCode 不是 Wiki 本体，而是 Wiki 的操作台。** 系统先浏览 Wiki 知识页确定方向，再回到原始文字、完整表格或原图，最后生成带 Evidence ID 的回答。

## 2. 构建阶段

```text
mmwiki-0.1 Source Package
  → 文本 Wiki 基线（正文 / OCR / Caption / 线性化代理）
  → 多模态增量（完整表格 / 公式 / 原图）
  → 稳定知识页 + WikiLink + Evidence 地图
  → Wiki 页面索引 + 文本 Evidence 索引 + 视觉 Evidence 索引
```

当前规模：**{snapshot['sources']} 个来源、{snapshot['items']} 个 Item、{snapshot['chunks']} 个文本 Chunk、{snapshot['assets']} 个视觉资源、{snapshot['pages']} 个稳定知识页**。

## 3. 查询阶段

```text
用户问题
  → Wiki 页面 BM25 / 页面向量（先找相关知识页）
  → Evidence 检索（再找具体 Item）
  → 回读原文 / rows-cells / 原图
  → 视觉语言模型回答
  → 返回 Evidence ID、模型、检索模式和耗时
```

## 4. 表格如何呈现

- 问题：**{table_case['question']}**
- Wiki 定位：{wiki_link(table_wiki or '知识页', table_wiki)}
- Evidence：`{table_evidence}`
- 原始表格资源：[在浏览器中打开表格截图]({media_url(table_asset)})

{markdown_table(table_item.get('table') or {})}

这里体现的不是“Caption 代替表格”，而是 Wiki 页面保存结论，同时 Evidence 层保留完整 `rows/cells` 和原图。

## 5. 图片如何呈现

- 问题：**{visual_case['question']}**
- Wiki 定位：{wiki_link(visual_wiki or '知识页', visual_wiki)}
- Evidence：`{visual_evidence}`
- 图片说明：{visual_hit.get('snippet', '')}

{evidence_image('Figure 4 原始 Evidence', visual_asset)}

图片问题在 `multimodal` 模式下会把原图交给视觉检索与视觉语言模型，而不是只阅读 Caption。

## 6. 接下来现场点击什么

1. 输入 `/wiki-table`，展示完整表格问答。
2. 输入 `/wiki-image`，展示原图理解问答。
3. 输入 `/wiki-compare`，展示文本基线与多模态增量指标。
4. 输入 `/wiki-refuse`，展示证据不足时不编造。
"""


def render_compare() -> str:
    staged = load_json(PROJECT_ROOT / "reports/staged-pipeline-benchmark-offline.json")
    text = staged.get("text_baseline", {})
    multi = staged.get("multimodal_enhancement", {})
    text_index = text.get("index", {})
    multi_index = multi.get("index", {})
    text_snapshot = text.get("snapshot", {})
    multi_snapshot = multi.get("snapshot", {})

    retrieval_rows = []
    for label, filename in [
        ("Lexical", "retrieval-40-lexical.json"),
        ("Hybrid", "retrieval-40-hybrid.json"),
        ("Multimodal", "retrieval-40-multimodal.json"),
    ]:
        report = load_json(PROJECT_ROOT / "reports" / filename)
        metrics = report.get("metrics", {})
        retrieval_rows.append(
            f"| {label} | {percent(metrics.get('recall_at_k'))} | {percent(metrics.get('mrr'))} | "
            f"{percent(metrics.get('top1_accuracy'))} | {number(metrics.get('latency_ms_mean'))} ms |"
        )

    qa_rows = []
    for label, filename in [
        ("Hybrid", "online-40-hybrid.json"),
        ("Multimodal", "online-40-multimodal.json"),
    ]:
        report = load_json(PROJECT_ROOT / "reports" / filename)
        metrics = report.get("metrics", {})
        qa_rows.append(
            f"| {label} | {percent(metrics.get('answerability_accuracy'))} | "
            f"{percent(metrics.get('concept_coverage_accuracy'))} | {percent(metrics.get('citation_accuracy'))} | "
            f"{number(metrics.get('latency_ms_mean'))} ms |"
        )

    return f"""# 文本 Wiki 基线与多模态增量对比

## 结论先行

多模态能力采用**增量加入**：保留文本 Wiki 和既有文本向量，只新增结构化表格、公式、原图和视觉向量。它没有把原系统推倒重建。

## 工程增量

| 指标 | 文本 Wiki 基线 | 多模态增量后 |
|---|---:|---:|
| 来源 | {text_snapshot.get('sources', 0)} | {multi_snapshot.get('sources', 0)} |
| 文本 Chunk | {text_snapshot.get('chunks', 0)} | {multi_snapshot.get('chunks', 0)} |
| 视觉资源 | {text_snapshot.get('assets', 0)} | {multi_snapshot.get('assets', 0)} |
| 复用文本向量 | {text_index.get('reused_text_records', 0)} | **{multi_index.get('reused_text_records', 0)}** |
| 新建文本向量 | {text_index.get('new_text_records', 0)} | **{multi_index.get('new_text_records', 0)}** |
| 新建视觉向量 | {text_index.get('new_visual_records', 0)} | **{multi_index.get('new_visual_records', 0)}** |
| 索引耗时 | {number(text_index.get('elapsed_ms'))} ms | {number(multi_index.get('elapsed_ms'))} ms |

## 40 题检索

| 模式 | Recall@5 | MRR | Top-1 | 平均延迟 |
|---|---:|---:|---:|---:|
{chr(10).join(retrieval_rows)}

## 40 题在线问答

| 模式 | 可回答判断 | 概念覆盖 | 引用命中 | 平均延迟 |
|---|---:|---:|---:|---:|
{chr(10).join(qa_rows)}

## 正确解读

- `auto` 是统一入口：默认走 BM25 + MinerU Caption，不隐式调用向量或 VLM。
- 显式打开向量检索后，普通语义问题可进入 `Hybrid`；同时打开 VLM 后，视觉问题才进入 `Multimodal`。
- 100% 文本向量复用说明增量架构有效，不代表所有问答指标都应该达到 100%。
- 离线工程基准验证增量复杂度；在线 40 题评测验证真实模型效果，两者不能混为一谈。
"""


def render_live_result(result: dict[str, Any]) -> str:
    question = result.get("question", "")
    retrieval = result.get("retrieval", {})
    navigation = retrieval.get("wiki_navigation", [])
    citations = result.get("citations", [])
    answer = normalize_math_markdown(str(result.get("answer") or "未生成回答"))
    lines = [
        "# 多模态 Wiki 现场问答",
        "",
        f"> **问题：** {question}",
        "",
        f"## 结论（最终回答）\n\n{answer}",
        "",
        "## Wiki 定位",
        "",
    ]
    if navigation:
        for page in navigation[:4]:
            channels = ", ".join(page.get("navigation_channels", []))
            channel_text = f" · `{channels}`" if channels else ""
            lines.append(
                f"- {wiki_link(page.get('title', page.get('path', 'Wiki 页面')), page.get('path', ''))}"
                f"{channel_text}：{page.get('summary', '')}"
            )
    else:
        lines.append("- 未返回 Wiki 导航页。")

    lines.extend(["", "## 原始 Evidence", ""])
    if citations:
        state = WikiPipeline(PROJECT_ROOT)._load_state()
        shown_assets: set[str] = set()
        for index, citation in enumerate(citations[:8], start=1):
            evidence_ids = citation.get("evidence_ids", [])
            evidence = evidence_ids[0] if evidence_ids else citation.get("chunk_id", "")
            source_id = citation.get("source_id", "")
            item_id = (citation.get("item_ids") or [""])[0]
            item = find_item(state, source_id, item_id)
            evidence_url = wiki_view_url(citation.get("path", ""))
            if item_id:
                evidence_url += "#" + quote(item_id, safe="-_.:")
            lines.extend(
                [
                    f"### Evidence {index}",
                    "",
                    f"- ID：`{evidence}`",
                    f"- 类型：`{','.join(citation.get('modalities', []))}`",
                    f"- 位置：[{citation.get('title', '打开原始 Evidence')}]({evidence_url})",
                ]
            )
            normalized_snippet = normalize_math_markdown(
                str(citation.get("snippet") or "")
            )
            snippet = normalize_math_markdown(" ".join(normalized_snippet.split()))
            if snippet:
                lines.extend(["", f"> 证据摘录：{snippet[:800]}"])
            if item.get("table"):
                lines.extend(["", markdown_table(item["table"]), ""])
            asset = citation.get("matched_asset_path") or (
                (citation.get("asset_paths") or [""])[0]
            )
            if asset and asset not in shown_assets and len(shown_assets) < 4:
                shown_assets.add(asset)
                lines.extend(
                    ["", evidence_image(f"Evidence {index} 原始视觉内容", asset), ""]
                )
            lines.append("")
    else:
        lines.append("- 没有足够 Evidence；不应把无依据内容写成确定事实。")

    usage = result.get("usage", {})
    fallback = retrieval.get("fallback_reason") or "无"
    lines.extend(
        [
            "",
            "## 运行信息",
            "",
            "| 字段 | 值 |",
            "|---|---|",
            f"| 检索模式 | 请求 `{retrieval.get('requested_mode', '')}` / 实际 `{retrieval.get('mode', '')}` |",
            f"| 回答模型 | `{result.get('model', '')}` |",
            f"| Wiki 导航 | `{retrieval.get('wiki_navigation_strategy', '未记录')}` |",
            f"| 回退 | {fallback} |",
            f"| 延迟 | {number(result.get('latency_ms'))} ms |",
            f"| Token | {usage.get('total_tokens', usage.get('total', '—'))} |",
            f"| Query ID | `{result.get('query_id', '')}` |",
        ]
    )
    return "\n".join(lines) + "\n"


def render_live(
    pipeline: WikiPipeline,
    question: str,
    mode: str,
    provider: str,
    dry_run: bool,
) -> str:
    selected_mode = mode
    if dry_run:
        return f"""# 现场问答执行计划

- 问题：{question}
- 检索模式：`{selected_mode}`
- 回答 Provider：`{provider}`
- 输出：Wiki 定位 → 原始 Evidence → 最终回答 → 模型与耗时
"""
    try:
        result = pipeline.query(question, 5, provider, None, selected_mode)
    except (PipelineError, ProviderError, ValueError) as exc:
        return f"""# 现场问答未执行完成

原因：`{exc}`

请先输入 `/wiki-check`。如果模型配置正常，再重试当前命令；不要改写 Source Package 或删除运行数据。
"""
    return render_live_result(result)


def render_questions() -> str:
    return """# 推荐现场问题

| 类型 | 问题 | 推荐命令/模式 | 证明什么 |
|---|---|---|---|
| 表格 | 开发测试阶段需要多少天、多少人、多少预算？ | `/wiki-table` · Auto | 默认 BM25 + Caption，回读完整 rows/cells |
| 图片 | Figure 4 中 ReToken 推理的数据流是什么？ | `/wiki-image` · Auto | VLM 开启时读取原图；关闭时明确回退 |
| 拒答 | Figure 4 中蓝色方框的 RGB 精确数值是多少？ | `/wiki-refuse` · Auto | 证据不足时不编造 |
| 自由问答 | 任意文档问题 | `/wiki-ask 你的问题` | Auto 根据特性开关选择实际路径 |

推荐顺序：`/wiki-demo` → `/wiki-table` → `/wiki-image` → `/wiki-compare` → `/wiki-refuse`。
"""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="OpenCode 多模态 Wiki 中文演示入口")
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="显示零基础入口")
    subparsers.add_parser("status", help="显示演示就绪状态")
    subparsers.add_parser("tour", help="显示完整构建和查询导览")
    subparsers.add_parser("compare", help="显示文本基线与多模态指标")
    subparsers.add_parser("questions", help="显示推荐演示问题")
    live = subparsers.add_parser("live", help="执行一次带引用的现场问答")
    live.add_argument("--case", choices=tuple(DEMO_CASES))
    live.add_argument("--question")
    live.add_argument(
        "--mode", choices=("auto", "lexical", "hybrid", "multimodal"), default="auto"
    )
    live.add_argument("--provider", choices=("api", "baseline"), default="api")
    live.add_argument("--dry-run", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    pipeline = WikiPipeline(PROJECT_ROOT)
    if args.command == "start":
        output = render_start(pipeline)
    elif args.command == "status":
        output = render_status(pipeline)
    elif args.command == "tour":
        output = render_tour(pipeline)
    elif args.command == "compare":
        output = render_compare()
    elif args.command == "questions":
        output = render_questions()
    elif args.command == "live":
        case = DEMO_CASES.get(args.case or "", {})
        question = args.question or case.get("question")
        if not question:
            print("error: live 需要 --case 或 --question", file=sys.stderr)
            return 2
        mode = case.get("mode", args.mode) if args.mode == "auto" else args.mode
        output = render_live(pipeline, question, mode, args.provider, args.dry_run)
    else:
        return 2
    print(output.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
