#!/usr/bin/env python3
"""Sequential, resumable Wiki compilation for a directory of Source Packages."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def package_id(package: Path) -> str:
    return str(read_json(package / "manifest.json").get("package_id") or package.name)


def api_stage_complete(state_path: Path, source_id: str, stage: str) -> bool:
    state = read_json(state_path)
    source = (state.get("sources") or {}).get(source_id) or {}
    record = (source.get("stages") or {}).get(stage) or {}
    return record.get("status") == "completed" and record.get("provider") == "api"


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def child_arguments(args: argparse.Namespace) -> list[str]:
    result = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(args.packages_root),
        "--runtime-root",
        str(args.runtime_root),
        "--stage",
        args.stage,
        "--provider",
        args.provider,
        "--log",
        str(args.log),
    ]
    if args.continue_on_error:
        result.append("--continue-on-error")
    if args.source_id_file:
        result.extend(["--source-id-file", str(args.source_id_file)])
    if args.visual_scope_file:
        result.extend(["--visual-scope-file", str(args.visual_scope_file)])
    return result


def detach(args: argparse.Namespace) -> int:
    output_path = args.log.with_suffix(".out.log")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("ab") as output:
        process = subprocess.Popen(
            child_arguments(args),
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(
        json.dumps(
            {
                "status": "started",
                "pid": process.pid,
                "progress_log": str(args.log),
                "output_log": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    app = project_root / "app.py"
    state_path = args.runtime_root / "state.json"
    visual_scope: dict[str, list[str]] = {}
    if args.visual_scope_file:
        raw_scope = read_json(args.visual_scope_file)
        visual_scope = {
            str(source_id): [str(item_id) for item_id in item_ids]
            for source_id, item_ids in raw_scope.items()
            if isinstance(item_ids, list)
        }
    packages = sorted(
        path.parent for path in args.packages_root.glob("*/manifest.json")
    )
    if args.source_id_file:
        selected = {
            line.strip()
            for line in args.source_id_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        packages = [path for path in packages if package_id(path) in selected]
        discovered = {package_id(path) for path in packages}
        missing = sorted(selected - discovered)
        if missing:
            raise SystemExit("未找到指定 Source Package：" + "、".join(missing))
    failures = 0
    for index, package in enumerate(packages, 1):
        source_id = package_id(package)
        if api_stage_complete(state_path, source_id, args.stage):
            entry = {
                "time": now(),
                "index": index,
                "total": len(packages),
                "package_id": source_id,
                "stage": args.stage,
                "status": "skipped_api_complete",
            }
            append_jsonl(args.log, entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
            continue

        command = [
            sys.executable,
            str(app),
            "--runtime-root",
            str(args.runtime_root),
            "ingest",
            str(package),
            "--provider",
            args.provider,
            "--stage",
            args.stage,
            "--force",
            "--vector-retrieval",
            "on",
        ]
        if args.stage == "multimodal":
            if args.visual_scope_file:
                scoped_item_ids = visual_scope.get(source_id, [])
                if not scoped_item_ids:
                    command.extend(["--vlm", "off"])
                else:
                    command.extend(["--vlm", "on"])
                for item_id in scoped_item_ids:
                    command.extend(["--visual-item-id", item_id])
            else:
                command.extend(["--vlm", "on"])
        started = now()
        completed = subprocess.run(
            command,
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            result = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except json.JSONDecodeError:
            result = {"raw_stdout": completed.stdout[-2000:]}
        entry = {
            "time": now(),
            "started_at": started,
            "index": index,
            "total": len(packages),
            "package_id": source_id,
            "stage": args.stage,
            "status": "completed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "result": result,
        }
        if completed.stderr.strip():
            entry["stderr"] = completed.stderr[-2000:]
        append_jsonl(args.log, entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if completed.returncode != 0:
            failures += 1
            if not args.continue_on_error:
                break
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按目录顺序、可断点续跑地编译 Source Package Wiki"
    )
    parser.add_argument("packages_root", type=Path)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--stage", choices=("text", "multimodal"), default="text")
    parser.add_argument("--provider", choices=("api",), default="api")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument(
        "--source-id-file",
        type=Path,
        help="每行一个 package_id；只处理清单中的来源",
    )
    parser.add_argument(
        "--visual-scope-file",
        type=Path,
        help="JSON 对象：package_id 到需增强 item_id 数组；仅用于 multimodal",
    )
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    args.packages_root = args.packages_root.expanduser().resolve()
    args.runtime_root = args.runtime_root.expanduser().resolve()
    args.log = (
        args.log.expanduser().resolve()
        if args.log
        else args.runtime_root / f"batch-compile-{args.stage}.jsonl"
    )
    if args.source_id_file:
        args.source_id_file = args.source_id_file.expanduser().resolve()
    if args.visual_scope_file:
        args.visual_scope_file = args.visual_scope_file.expanduser().resolve()
        if args.stage != "multimodal":
            parser.error("--visual-scope-file 只适用于 --stage multimodal")
    return args


def main() -> int:
    args = parse_args()
    if not args.packages_root.is_dir():
        raise SystemExit(f"Source Package 目录不存在：{args.packages_root}")
    return detach(args) if args.detach else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
