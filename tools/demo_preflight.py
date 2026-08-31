from __future__ import annotations

import json
import shutil
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
        pipeline.vault / "Home.md",
        pipeline.vault / "wiki/index.md",
        pipeline.vault / "wiki/overview.md",
        PROJECT_ROOT / ".opencode/skills/multimodal-wiki/SKILL.md",
        PROJECT_ROOT / "opencode.json",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required if not path.is_file()]
    health: dict[str, object]
    try:
        with urllib.request.urlopen("http://127.0.0.1:19828/api/v1/ping", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        health = {"status": "offline", "hint": "运行 python3 app.py api"}
    state = pipeline._load_state()
    retrieval = pipeline.retrieval_status()
    opencode_cli = shutil.which("opencode") or str(
        Path.home() / ".opencode/bin/opencode"
    )
    opencode_ready = Path(opencode_cli).is_file()
    project_integration_ready = all(
        path.is_file()
        for path in (
            PROJECT_ROOT / ".opencode/skills/multimodal-wiki/SKILL.md",
            PROJECT_ROOT / ".opencode/tools/wiki.ts",
            PROJECT_ROOT / ".opencode/commands/wiki-check.md",
            PROJECT_ROOT / ".opencode/commands/wiki-ask.md",
            PROJECT_ROOT / "opencode.json",
        )
    )
    enhanced_retrieval_ready = (
        retrieval["text_ready"] and retrieval["visual_ready"]
    )
    result = {
        "ready": (
            lint["status"] == "passed"
            and provider.configured
            and enhanced_retrieval_ready
            and project_integration_ready
            and not missing
        ),
        "sources": len(state.get("sources", {})),
        "wiki_pages": len(state.get("pages", {})),
        "vision_model": provider.model or None,
        "provider_configured": provider.configured,
        "enhanced_retrieval_ready": enhanced_retrieval_ready,
        "opencode": {
            "cli": opencode_cli,
            "cli_ready": opencode_ready,
            "project_integration_ready": project_integration_ready,
            "desktop_app": str(Path("/Applications/OpenCode.app")),
            "desktop_ready": Path("/Applications/OpenCode.app").is_dir(),
            "skill": ".opencode/skills/multimodal-wiki/SKILL.md",
        },
        "retrieval": retrieval,
        "api": health,
        "lint": lint,
        "missing_demo_files": missing,
        "runtime_root": str(pipeline.runtime),
        "vault": str(pipeline.vault),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
