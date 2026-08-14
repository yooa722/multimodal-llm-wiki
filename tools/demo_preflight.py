from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mmwiki.pipeline import WikiPipeline
from mmwiki.provider import OpenAICompatibleProvider


def main() -> int:
    pipeline = WikiPipeline(PROJECT_ROOT)
    lint = pipeline.lint()
    provider = OpenAICompatibleProvider(PROJECT_ROOT, "vision")
    required = [
        PROJECT_ROOT / "runtime/vault/Home.md",
        PROJECT_ROOT / "runtime/vault/wiki/index.md",
        PROJECT_ROOT / "runtime/vault/wiki/overview.md",
        PROJECT_ROOT / "runtime/vault/.obsidian/plugins/multimodal-wiki-query/main.js",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.is_file()]
    health: dict[str, object]
    try:
        with urllib.request.urlopen("http://127.0.0.1:19828/api/v1/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        health = {"status": "offline", "hint": "运行 python3 app.py api"}
    state = pipeline._load_state()
    retrieval = pipeline.retrieval_status()
    result = {
        "ready": lint["status"] == "passed" and provider.configured and not missing,
        "sources": len(state.get("sources", {})),
        "wiki_pages": len(state.get("pages", {})),
        "vision_model": provider.model or None,
        "provider_configured": provider.configured,
        "enhanced_retrieval_ready": (
            retrieval["text_ready"] and retrieval["visual_ready"]
        ),
        "retrieval": retrieval,
        "api": health,
        "lint": lint,
        "missing_demo_files": missing,
        "vault": str(PROJECT_ROOT / "runtime/vault"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
