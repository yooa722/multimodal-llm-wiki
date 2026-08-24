"""Lightweight feature switches for incremental multimodal Wiki support."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .provider import read_dotenv


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean feature flag: {value!r}")


def _override(value: Optional[str], current: bool) -> bool:
    if value is None:
        return current
    normalized = str(value).strip().lower()
    if normalized not in {"on", "off"}:
        raise ValueError(f"feature override must be on or off: {value!r}")
    return normalized == "on"


@dataclass(frozen=True)
class FeatureConfig:
    """Runtime switches shared by CLI, pipeline and retrieval."""

    enable_vlm: bool = False
    enable_vector_retrieval: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "enable_vlm": self.enable_vlm,
            "enable_vector_retrieval": self.enable_vector_retrieval,
        }


@dataclass(frozen=True)
class VisualIntent:
    """Explain why a question should (or should not) read image pixels."""

    is_visual: bool
    categories: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if not self.is_visual:
            return "未发现需要读取图片像素的明确视觉意图"
        labels = {
            "visual_reference": "明确引用图片",
            "color_depth": "颜色或深浅",
            "position": "位置或方向",
            "connection": "箭头、流程或连接",
            "trend": "曲线、柱形或趋势",
            "scene": "物体、人物或场景",
            "spatial_quantity": "数量或空间关系",
        }
        return "检测到" + "、".join(labels[value] for value in self.categories)

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_visual": self.is_visual,
            "categories": list(self.categories),
            "matched_terms": list(self.matched_terms),
            "reason": self.reason,
        }


_VISUAL_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "visual_reference",
        re.compile(
            r"图中|原图|图片|图像|照片|画面|截图|流程图|示意图|架构图|"
            r"\b(?:figure|fig\.?|image|photo|screenshot|diagram|chart|plot)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "color_depth",
        re.compile(
            r"(?:红|橙|黄|绿|青|蓝|紫|黑|白|灰|粉|棕)(?:色|线|框|块|区域)|"
            r"颜色|色彩|深色|浅色|明暗|亮度|深浅|"
            r"\b(?:red|orange|yellow|green|blue|purple|black|white|gray|grey|pink|brown|"
            r"color|colour|dark|light|brightness)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "position",
        re.compile(
            r"左侧|右侧|上方|下方|顶部|底部|中间|中央|角落|"
            r"位置|方位|左右|上下|"
            r"\b(?:left|right|above|below|top|bottom|middle|center|corner|position)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "connection",
        re.compile(
            r"箭头|指向|连线|连接关系|相连|数据流|流向|流程图|框图|拓扑|"
            r"\b(?:arrow|point(?:s|ing)?\s+to|connect(?:ed|ion)?|link|flowchart|data\s+flow|topology)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "trend",
        re.compile(
            r"曲线|折线|柱形|柱状|趋势图|走势图|坐标轴|"
            r"\b(?:curve|line\s+chart|bar\s+chart|histogram|axis)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "scene",
        re.compile(
            r"物体|物品|场景|背景中|前景中|"
            r"\b(?:object|scene|foreground|background)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "spatial_quantity",
        re.compile(
            r"空间关系|相对位置|相邻|重叠|遮挡|包含关系|排列|间距|"
            r"\b(?:spatial|adjacent|overlap|occlusion|arrangement)\b",
            re.IGNORECASE,
        ),
    ),
)


def detect_visual_intent(question: str) -> VisualIntent:
    """Classify explicit pixel-level intent without treating every ``图`` as visual."""

    text = str(question or "").strip()
    categories: list[str] = []
    matched_terms: list[str] = []
    for category, pattern in _VISUAL_INTENT_PATTERNS:
        matches = [match.group(0) for match in pattern.finditer(text)]
        if not matches:
            continue
        categories.append(category)
        matched_terms.extend(matches)

    has_visual_anchor = any(
        value in categories
        for value in (
            "visual_reference",
            "color_depth",
            "position",
            "connection",
            "trend",
            "scene",
        )
    )
    contextual_patterns = (
        (
            "connection",
            r"流程|过程|\b(?:process|pipeline)\b",
        ),
        (
            "trend",
            r"趋势|走势|峰值|谷值|上升|下降|波动|\b(?:trend|peak|valley|rising|falling)\b",
        ),
        (
            "scene",
            r"人物|人像|车辆|动物|建筑|\b(?:person|people|vehicle|animal|building)\b",
        ),
        (
            "spatial_quantity",
            r"分布|距离|\b(?:distance|distribution)\b",
        ),
    )
    if has_visual_anchor:
        for category, pattern in contextual_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match and category not in categories:
                categories.append(category)
                matched_terms.append(match.group(0))

    # 数量词本身常用于普通事实问答；只有与图片或场景同时出现时才属于视觉计数。
    if (
        any(value in categories for value in ("visual_reference", "scene"))
        and re.search(r"多少|几个|几人|数量|数一数|\bhow\s+many\b", text, re.IGNORECASE)
        and "spatial_quantity" not in categories
    ):
        categories.append("spatial_quantity")
        matched_terms.append("视觉数量")

    return VisualIntent(
        is_visual=bool(categories),
        categories=tuple(categories),
        matched_terms=tuple(dict.fromkeys(matched_terms)),
    )


@dataclass(frozen=True)
class VisualProcessingPolicy:
    """Cost-aware persistent processing plan for one visual resource."""

    resource_type: str
    primary_representation: str
    run_caption: bool
    run_ocr: bool
    ocr_policy: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "primary_representation": self.primary_representation,
            "run_caption": self.run_caption,
            "run_ocr": self.run_ocr,
            "ocr_policy": self.ocr_policy,
            "reason": self.reason,
        }


def resolve_visual_processing_policy(
    *,
    item_type: str,
    has_structured_table: bool = False,
    has_latex: bool = False,
    caption: str = "",
    breadcrumb: str = "",
    metadata: dict[str, Any] | None = None,
) -> VisualProcessingPolicy:
    """Choose persistent OCR/Caption work from source type and parser metadata."""

    normalized_type = str(item_type or "").strip().casefold()
    metadata = metadata if isinstance(metadata, dict) else {}
    declared_type = str(
        metadata.get("visual_type")
        or metadata.get("resource_type")
        or metadata.get("image_type")
        or ""
    ).strip().casefold()
    effective_type = declared_type or normalized_type
    context = " ".join((str(caption or ""), str(breadcrumb or ""))).casefold()

    if effective_type in {"equation", "formula", "formula_screenshot"} or has_latex:
        return VisualProcessingPolicy(
            "formula",
            "latex",
            False,
            False,
            "disabled",
            "公式优先保留 LaTeX，不默认生成普通 Caption 或 OCR",
        )
    if normalized_type == "table" or has_structured_table or effective_type in {
        "table",
        "table_screenshot",
        "table-image",
    }:
        return VisualProcessingPolicy(
            "table_screenshot",
            "structured_table",
            False,
            True,
            "assistive",
            "表格以 rows/cells/html 结构为准，OCR 仅用于辅助核对",
        )
    if effective_type in {"page", "page_image", "page_screenshot", "screenshot"}:
        return VisualProcessingPolicy(
            "page_screenshot",
            "ocr+vlm_caption",
            True,
            True,
            "always",
            "页面截图同时需要文字识别与版面视觉理解",
        )

    if declared_type in {"natural", "natural_image", "photo", "scene"}:
        ocr_required = bool(
            metadata.get("ocr_required")
            or metadata.get("contains_text")
            or metadata.get("text_dense")
        )
        return VisualProcessingPolicy(
            "natural_image",
            "vlm_caption",
            True,
            ocr_required,
            "always" if ocr_required else "on_demand",
            (
                "解析元数据标记图片含关键文字，因此在 Caption 外同步执行 OCR"
                if ocr_required
                else "自然图片优先生成 VLM Caption，OCR 保留为按需能力"
            ),
        )

    if re.search(
        r"页面截图|整页截图|网页截图|\b(?:page\s+screenshot|screenshot\s+of\s+(?:a\s+)?page)\b",
        context,
        re.IGNORECASE,
    ):
        return VisualProcessingPolicy(
            "page_screenshot",
            "ocr+vlm_caption",
            True,
            True,
            "always",
            "页面截图同时需要文字识别与版面视觉理解",
        )
    if re.search(
        r"表格截图|表截图|\b(?:table\s+screenshot|screenshot\s+of\s+(?:a\s+)?table)\b",
        context,
        re.IGNORECASE,
    ):
        return VisualProcessingPolicy(
            "table_screenshot",
            "structured_table",
            False,
            True,
            "assistive",
            "表格截图优先使用解析层结构化结果，OCR 仅用于辅助核对",
        )

    diagram_types = {
        "chart",
        "plot",
        "diagram",
        "flowchart",
        "flow_chart",
        "architecture_diagram",
    }
    diagram_context = re.search(
        r"流程图|架构图|示意图|框图|数据流|折线|曲线|柱状|趋势|"
        r"\b(?:pipeline|flowchart|diagram|architecture|chart|plot|curve|bar\s+chart|data\s+flow)\b",
        context,
        re.IGNORECASE,
    )
    if effective_type in diagram_types or normalized_type == "chart" or diagram_context:
        return VisualProcessingPolicy(
            "diagram_chart",
            "ocr+vlm_caption",
            True,
            True,
            "always",
            "流程图或图表同时保留图中文字和整体视觉语义",
        )

    ocr_required = bool(
        metadata.get("ocr_required")
        or metadata.get("contains_text")
        or metadata.get("text_dense")
    )
    return VisualProcessingPolicy(
        "natural_image",
        "vlm_caption",
        True,
        ocr_required,
        "always" if ocr_required else "on_demand",
        (
            "解析元数据标记图片含关键文字，因此在 Caption 外同步执行 OCR"
            if ocr_required
            else "自然图片优先生成 VLM Caption，OCR 保留为按需能力"
        ),
    )


def load_feature_config(
    root: Path,
    *,
    vlm: Optional[str] = None,
    vector_retrieval: Optional[str] = None,
) -> FeatureConfig:
    """Load .env defaults and apply per-command ``on|off`` overrides."""

    dotenv = read_dotenv(Path(root) / ".env")
    vlm_value = os.environ.get("MMWIKI_ENABLE_VLM", dotenv.get("MMWIKI_ENABLE_VLM"))
    vector_value = os.environ.get(
        "MMWIKI_ENABLE_VECTOR_RETRIEVAL",
        dotenv.get("MMWIKI_ENABLE_VECTOR_RETRIEVAL"),
    )
    return FeatureConfig(
        enable_vlm=_override(vlm, _as_bool(vlm_value, False)),
        enable_vector_retrieval=_override(
            vector_retrieval,
            _as_bool(vector_value, False),
        ),
    )


def resolve_query_mode(
    requested: str,
    visual_intent: bool,
    config: FeatureConfig,
) -> str:
    """Resolve a requested mode without silently enabling expensive services."""

    if requested not in {"auto", "lexical", "hybrid", "multimodal"}:
        raise ValueError(f"unsupported retrieval mode: {requested}")
    if requested == "lexical":
        return "lexical"
    if not config.enable_vector_retrieval:
        return "lexical"
    if requested == "hybrid":
        return "hybrid"
    if requested == "multimodal":
        return "multimodal" if config.enable_vlm else "hybrid"
    if visual_intent and config.enable_vlm:
        return "multimodal"
    return "hybrid"
