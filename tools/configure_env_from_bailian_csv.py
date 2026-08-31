from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从阿里云百炼导出的键值 CSV 安全生成项目 .env"
    )
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("env_path", type=Path)
    args = parser.parse_args()

    rows = list(csv.reader(args.csv_path.open(encoding="utf-8-sig", newline="")))
    values = {
        str(row[0]).strip(): str(row[1]).strip()
        for row in rows
        if len(row) >= 2 and str(row[0]).strip()
    }
    key = values.get("apiKey", "")
    compatible = values.get("openAiCompatible", "").rstrip("/")
    if not key:
        raise ValueError("CSV 中缺少 apiKey")
    if not compatible.startswith("https://"):
        raise ValueError("CSV 中缺少有效的 openAiCompatible 地址")
    content = (
        f"MMWIKI_API_BASE_URL={compatible}\n"
        f"MMWIKI_API_KEY={key}\n"
        "MMWIKI_BUILD_MODEL=qwen3.7-plus\n"
        "MMWIKI_VISION_MODEL=qwen3-vl-plus\n"
        "MMWIKI_TEXT_EMBEDDING_MODEL=text-embedding-v4\n"
        "MMWIKI_EMBEDDING_DIMENSION=512\n"
        "MMWIKI_TIMEOUT=180\n"
        "MMWIKI_MAX_IMAGES=4\n"
        "MMWIKI_MAX_OUTPUT_TOKENS=3000\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(args.env_path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(args.env_path, 0o600)
    print(f"created {args.env_path} (key redacted, mode 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
