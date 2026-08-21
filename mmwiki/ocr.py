"""Small native HTTP client for the Bailian Qwen3.5-OCR endpoint."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None

from .provider import ProviderError, read_dotenv


OCR_ENDPOINT_SUFFIX = "/api/v1/services/aigc/multimodal-generation/generation"


def _service_root(base_url: str) -> str:
    value = str(base_url or "").rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-api/v1", "/api/v1"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def derive_ocr_url(base_url: str, explicit_url: str = "") -> str:
    """Resolve the native OCR URL without adding a DashScope dependency."""

    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    root = _service_root(base_url)
    if not root:
        return ""
    if root.endswith("/services/aigc/multimodal-generation/generation"):
        return root
    return root + OCR_ENDPOINT_SUFFIX


def build_ocr_payload(
    model: str,
    data_url: str,
    task: str,
    min_pixels: int,
    max_pixels: int,
) -> dict[str, Any]:
    return {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "image": data_url,
                            "min_pixels": min_pixels,
                            "max_pixels": max_pixels,
                            "enable_rotate": False,
                        }
                    ],
                }
            ]
        },
        "parameters": {"ocr_options": {"task": task}},
    }


def extract_ocr_text(payload: dict[str, Any]) -> str:
    """Extract plain text from the native OCR response, not JSON mode."""

    choices = ((payload.get("output") or {}).get("choices") or [])
    parts: list[str] = []
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else {}
        content = message.get("content") if isinstance(message, dict) else []
        if isinstance(content, str):
            parts.append(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and str(part.get("text") or "").strip():
                parts.append(str(part["text"]).strip())
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise ProviderError("Qwen3.5-OCR 返回了空文本")
    return text


class QwenOCRProvider:
    """串行、单图调用的 Qwen3.5-OCR Provider。"""

    def __init__(self, root: Path):
        values = read_dotenv(Path(root) / ".env")

        def setting(name: str, default: str = "") -> str:
            return os.environ.get(name, values.get(name, default)).strip()

        base_url = setting("MMWIKI_API_BASE_URL")
        self.key = setting("MMWIKI_API_KEY")
        self.url = derive_ocr_url(base_url, setting("MMWIKI_OCR_API_URL"))
        self.model = setting("MMWIKI_OCR_MODEL", "qwen3.5-ocr")
        self.task = setting("MMWIKI_OCR_TASK", "text_recognition")
        self.min_pixels = int(setting("MMWIKI_OCR_MIN_PIXELS", "3072"))
        self.max_pixels = int(setting("MMWIKI_OCR_MAX_PIXELS", "8388608"))
        self.timeout = int(setting("MMWIKI_TIMEOUT", "60"))

    @property
    def configured(self) -> bool:
        return bool(self.key and self.url and self.model and self.task)

    def recognize(self, data_url: str) -> tuple[str, dict[str, Any]]:
        if not self.configured:
            raise ProviderError("Qwen3.5-OCR API 尚未配置")
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                build_ocr_payload(
                    self.model,
                    data_url,
                    self.task,
                    self.min_pixels,
                    self.max_pixels,
                ),
                ensure_ascii=False,
            ).encode("utf-8"),
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
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ProviderError(f"Qwen3.5-OCR API HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Qwen3.5-OCR API 请求失败：{exc.reason}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("Qwen3.5-OCR API 返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Qwen3.5-OCR API 返回格式错误")
        return extract_ocr_text(payload), payload.get("usage") or {}
