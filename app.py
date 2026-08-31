from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mmwiki.api import serve
from mmwiki.contracts import ContractError
from mmwiki.pipeline import INGEST_STAGES, PipelineError, WikiPipeline
from mmwiki.provider import ProviderError
from mmwiki.retrieval import RETRIEVAL_MODES


ROOT = Path(__file__).resolve().parent
QUERY_MODES = ("auto",) + RETRIEVAL_MODES


def add_feature_flags(command: argparse.ArgumentParser) -> None:
    command.add_argument("--vlm", choices=("on", "off"), help="临时覆盖 VLM 开关")
    command.add_argument(
        "--vector-retrieval",
        choices=("on", "off"),
        help="临时覆盖向量检索开关",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Multimodal Wiki Demo")
    result.add_argument(
        "--runtime-root",
        type=Path,
        help="使用独立 Runtime；必须放在子命令之前，例如 --runtime-root runtime/official ingest ...",
    )
    commands = result.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="校验解析组 mmwiki-0.1 package")
    validate.add_argument("package", type=Path)

    ingest = commands.add_parser("ingest", help="从 package 分阶段构建多模态 LLM Wiki")
    ingest.add_argument("package", type=Path)
    ingest.add_argument("--provider", choices=("baseline", "api"), default="baseline")
    ingest.add_argument("--force", action="store_true", help="显式重建相同版本")
    ingest.add_argument(
        "--stage",
        choices=INGEST_STAGES,
        default="all",
        help="text 先构建文本 Wiki；multimodal 只增量加入表格、图片和公式；all 依次执行两阶段",
    )
    ingest.add_argument(
        "--full-scale",
        action="store_true",
        help="按页分析全部视觉资源后统一编译 Wiki（仅 api）",
    )
    ingest.add_argument(
        "--visual-item-id",
        action="append",
        default=[],
        help="multimodal 阶段只对指定 item 调用 OCR/VLM；全部多模态内容仍进入 Wiki",
    )
    add_feature_flags(ingest)

    ingest_wiki = commands.add_parser(
        "ingest-wiki",
        help="导入已有本地 Markdown Wiki，生成只读派生多模态 Wiki",
    )
    ingest_wiki.add_argument("wiki_root", type=Path)
    ingest_wiki.add_argument("--caption-package", type=Path, required=True)
    add_feature_flags(ingest_wiki)

    search = commands.add_parser("search", help="执行 Wiki 导航与 Evidence chunk 检索")
    search.add_argument("question")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--source-id", action="append", default=[])
    search.add_argument(
        "--retrieval-mode", choices=QUERY_MODES, default="auto"
    )
    add_feature_flags(search)

    query = commands.add_parser("query", help="执行带引用的图文问答")
    query.add_argument("question")
    query.add_argument("--top-k", type=int, default=5)
    query.add_argument("--source-id", action="append", default=[])
    query.add_argument(
        "--retrieval-mode", choices=QUERY_MODES, default="auto"
    )
    query.add_argument(
        "--provider",
        choices=("api", "baseline"),
        default="api",
        help="默认调用在线模型；是否发送图片由 --vlm/配置决定，baseline 只用于开发诊断",
    )
    add_feature_flags(query)

    status = commands.add_parser("wiki-status", help="显示 Wiki 与多模态功能开关状态")
    add_feature_flags(status)

    commands.add_parser("lint", help="检查 Wiki 页面、资源和状态")
    commands.add_parser(
        "refresh-pages",
        help="本地刷新知识页格式和多模态 Evidence，不调用模型",
    )
    curate = commands.add_parser(
        "curate",
        help="裁剪活跃 Obsidian/检索语料，保留不可变 Raw 与查询历史",
    )
    curate.add_argument("--keep", action="append", required=True)
    curate.add_argument(
        "--apply",
        action="store_true",
        help="实际执行；省略时只输出将要移除的内容",
    )
    edit_wiki = commands.add_parser(
        "edit-wiki",
        help="应用人工复核的本地 Wiki 页面计划，不调用外部模型",
    )
    edit_wiki.add_argument("package", type=Path)
    edit_wiki.add_argument("plan", type=Path)

    index = commands.add_parser("build-index", help="构建文本与多模态检索索引")
    index.add_argument(
        "--text-only", action="store_true", help="只构建文本向量索引"
    )
    index.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="仅重建指定来源并与版本一致的现有索引安全合并，可重复使用",
    )
    add_feature_flags(index)
    wiki_index = commands.add_parser(
        "build-wiki-index",
        help="只构建 Wiki 页面语义索引，保留现有文本与视觉 Evidence 向量",
    )
    add_feature_flags(wiki_index)
    commands.add_parser(
        "migrate-index",
        help="严格校验后把旧索引元数据本地升级到当前版本，不调用外部模型",
    )

    api = commands.add_parser("api", help="启动供 OpenCode/其他客户端调用的本地查询 API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=19828)
    return result


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    args = parser().parse_args()
    try:
        pipeline = WikiPipeline(ROOT, runtime_root=args.runtime_root)
        pipeline.configure_features(
            vlm=getattr(args, "vlm", None),
            vector_retrieval=getattr(args, "vector_retrieval", None),
        )
        if args.command == "validate":
            output(pipeline.validate(args.package))
        elif args.command == "ingest":
            output(
                pipeline.ingest(
                    args.package,
                    args.provider,
                    args.force,
                    args.full_scale,
                    args.stage,
                    set(args.visual_item_id) if args.visual_item_id else None,
                )
            )
        elif args.command == "ingest-wiki":
            output(
                pipeline.ingest_existing_wiki(
                    args.wiki_root,
                    args.caption_package,
                )
            )
        elif args.command == "search":
            output(
                pipeline.search_with_trace(
                    args.question,
                    args.top_k,
                    set(args.source_id) or None,
                    args.retrieval_mode,
                )
            )
        elif args.command == "query":
            output(
                pipeline.query(
                    args.question,
                    args.top_k,
                    args.provider,
                    set(args.source_id) or None,
                    args.retrieval_mode,
                )
            )
        elif args.command == "wiki-status":
            output(
                {
                    "feature_config": pipeline.features.as_dict(),
                    "retrieval": pipeline.retrieval_status(),
                }
            )
        elif args.command == "lint":
            output(pipeline.lint())
        elif args.command == "refresh-pages":
            output(pipeline.refresh_wiki_pages())
        elif args.command == "curate":
            output(pipeline.curate_sources(set(args.keep), args.apply))
        elif args.command == "edit-wiki":
            output(pipeline.apply_reviewed_wiki_plan(args.package, args.plan))
        elif args.command == "build-index":
            output(
                pipeline.build_retrieval_index(
                    not args.text_only,
                    set(args.source_id) or None,
                )
            )
        elif args.command == "build-wiki-index":
            output(pipeline.build_wiki_page_index())
        elif args.command == "migrate-index":
            output(pipeline.migrate_retrieval_index())
        elif args.command == "api":
            serve(ROOT, args.host, args.port, args.runtime_root)
        return 0
    except (ContractError, PipelineError, ProviderError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
