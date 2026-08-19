"""Lightweight feature switches for incremental multimodal Wiki support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
