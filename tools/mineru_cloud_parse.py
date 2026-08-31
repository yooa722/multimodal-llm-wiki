from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mmwiki.provider import read_dotenv  # noqa: E402
from tools.mineru_to_package import (  # noqa: E402
    convert_content_list,
    discover_content_lists,
)


DEFAULT_API_BASE = "https://mineru.net/api/v4"
SUPPORTED_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".webp",
    ".gif",
    ".bmp",
}
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_BATCH_FILES = 200
MAX_ZIP_FILES = 50_000
MAX_ZIP_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024


class MinerUCloudError(RuntimeError):
    pass


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _config() -> dict[str, str]:
    values = read_dotenv(ROOT / ".env")
    for key in ("MINERU_API_TOKEN", "MINERU_API_BASE_URL"):
        if os.environ.get(key):
            values[key] = str(os.environ[key])
    return values


def collect_inputs(paths: Iterable[Path]) -> list[Path]:
    selected: dict[Path, None] = {}
    for value in paths:
        path = value.expanduser().resolve()
        if path.is_dir():
            candidates = sorted(
                child
                for child in path.rglob("*")
                if child.is_file() and child.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            candidates = [path]
        else:
            raise MinerUCloudError(f"不支持或不存在的输入：{path}")
        for candidate in candidates:
            size = candidate.stat().st_size
            if size <= 0:
                raise MinerUCloudError(f"文件为空：{candidate}")
            if size > MAX_FILE_BYTES:
                raise MinerUCloudError(f"文件超过 MinerU 200MB 限制：{candidate}")
            selected[candidate] = None
    result = list(selected)
    if not result:
        raise MinerUCloudError("没有找到可上传的文档")
    if len(result) > MAX_BATCH_FILES:
        raise MinerUCloudError(f"单批最多上传 {MAX_BATCH_FILES} 个文件，当前为 {len(result)}")
    return result


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read(2048).decode("utf-8", errors="replace")
        raise MinerUCloudError(f"MinerU HTTP {exc.code}：{message}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MinerUCloudError(f"MinerU 请求失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise MinerUCloudError("MinerU 返回值不是 JSON 对象")
    if payload.get("code") != 0:
        raise MinerUCloudError(f"MinerU API 拒绝请求：{payload.get('msg') or payload.get('code')}")
    return payload


def _put_file(url: str, path: Path, *, timeout: float = 600) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MinerUCloudError("MinerU 返回了无效的上传地址")
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    kwargs: dict[str, Any] = {"host": parsed.hostname, "port": parsed.port, "timeout": timeout}
    if parsed.scheme == "https":
        kwargs["context"] = _ssl_context()
    connection = connection_class(**kwargs)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        connection.putrequest("PUT", target)
        connection.putheader("Content-Length", str(path.stat().st_size))
        connection.endheaders()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
        response = connection.getresponse()
        response.read()
        if not 200 <= response.status < 300:
            raise MinerUCloudError(
                f"上传到 MinerU 临时存储失败：HTTP {response.status}"
            )
    except (OSError, http.client.HTTPException) as exc:
        raise MinerUCloudError(f"上传文件失败：{path.name}：{exc}") from exc
    finally:
        connection.close()


def _download(url: str, target: Path, *, timeout: float = 600) -> None:
    request = urllib.request.Request(url, headers={"Accept": "application/zip"})
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MinerUCloudError(f"下载 MinerU 结果失败：{target.name}：{exc}") from exc


def safe_extract_zip(archive: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    root = output.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ZIP_FILES:
            raise MinerUCloudError("MinerU ZIP 文件数量异常，已拒绝解压")
        if sum(member.file_size for member in members) > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise MinerUCloudError("MinerU ZIP 解压后体积异常，已拒绝解压")
        for member in members:
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise MinerUCloudError("MinerU ZIP 包含符号链接，已拒绝解压")
            relative = Path(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise MinerUCloudError("MinerU ZIP 包含路径逃逸，已拒绝解压")
            target = (root / relative).resolve()
            if target != root and root not in target.parents:
                raise MinerUCloudError("MinerU ZIP 包含越界路径，已拒绝解压")
        bundle.extractall(root)


def _task_data_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"mmwiki-{digest.hexdigest()[:20]}"


def _source_title(filename: str) -> str:
    """Strip repeated supported extensions while preserving the human title."""
    value = Path(filename).name
    while Path(value).suffix.lower() in SUPPORTED_SUFFIXES:
        value = Path(value).stem
    return value.strip() or Path(filename).stem


def submit_batch(
    inputs: list[Path],
    *,
    token: str,
    api_base: str,
    model_version: str,
    language: str,
    enable_ocr: bool,
    enable_table: bool,
    enable_formula: bool,
    page_ranges: str,
) -> tuple[str, list[str]]:
    files: list[dict[str, Any]] = []
    for path in inputs:
        record: dict[str, Any] = {
            "name": path.name,
            "data_id": _task_data_id(path),
            "is_ocr": enable_ocr,
        }
        if page_ranges:
            record["page_ranges"] = page_ranges
        files.append(record)
    payload = _request_json(
        "POST",
        f"{api_base.rstrip('/')}/file-urls/batch",
        token=token,
        body={
            "files": files,
            "model_version": model_version,
            "language": language,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
        },
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MinerUCloudError("MinerU 上传响应缺少 data")
    batch_id = str(data.get("batch_id") or "")
    urls = data.get("file_urls")
    if not batch_id or not isinstance(urls, list) or len(urls) != len(inputs):
        raise MinerUCloudError("MinerU 上传响应缺少 batch_id 或文件地址")
    return batch_id, [str(value) for value in urls]


def wait_for_batch(
    batch_id: str,
    *,
    token: str,
    api_base: str,
    poll_interval: float,
    timeout: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        payload = _request_json(
            "GET",
            f"{api_base.rstrip('/')}/extract-results/batch/{batch_id}",
            token=token,
        )
        data = payload.get("data")
        results = data.get("extract_result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise MinerUCloudError("MinerU 任务响应缺少 extract_result")
        normalized = [value for value in results if isinstance(value, dict)]
        states = {str(value.get("state") or "") for value in normalized}
        if normalized and states <= {"done", "failed"}:
            return normalized
        if time.monotonic() >= deadline:
            raise MinerUCloudError(f"等待 MinerU 任务超时：{batch_id}")
        time.sleep(max(1.0, min(poll_interval, 30.0)))


def parse_remote(
    inputs: list[Path],
    output_root: Path,
    package_root: Path,
    *,
    token: str,
    api_base: str = DEFAULT_API_BASE,
    model_version: str = "vlm",
    language: str = "ch",
    enable_ocr: bool = False,
    enable_table: bool = True,
    enable_formula: bool = True,
    page_ranges: str = "",
    poll_interval: float = 5,
    timeout: float = 3600,
) -> dict[str, Any]:
    batch_id, upload_urls = submit_batch(
        inputs,
        token=token,
        api_base=api_base,
        model_version=model_version,
        language=language,
        enable_ocr=enable_ocr,
        enable_table=enable_table,
        enable_formula=enable_formula,
        page_ranges=page_ranges,
    )
    for path, url in zip(inputs, upload_urls, strict=True):
        _put_file(url, path)

    batch_root = output_root.expanduser().resolve() / f"batch-{batch_id}"
    if batch_root.exists():
        raise MinerUCloudError(f"输出批次目录已经存在：{batch_root}")
    batch_root.mkdir(parents=True)
    results = wait_for_batch(
        batch_id,
        token=token,
        api_base=api_base,
        poll_interval=poll_interval,
        timeout=timeout,
    )
    completed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for index, result in enumerate(results, 1):
        name = str(result.get("file_name") or f"document-{index}")
        if result.get("state") != "done":
            failed.append({"file": name, "error": str(result.get("err_msg") or "解析失败")})
            continue
        zip_url = str(result.get("full_zip_url") or "")
        if not zip_url:
            failed.append({"file": name, "error": "解析完成但没有 ZIP 下载地址"})
            continue
        document_root = batch_root / f"{index:03d}-{Path(name).stem}"
        archive = batch_root / f"{index:03d}-{Path(name).stem}.zip"
        _download(zip_url, archive)
        safe_extract_zip(archive, document_root)
        completed.append({"file": name, "output": str(document_root)})

    if not completed:
        raise MinerUCloudError(f"本批次没有成功解析的文件：{failed}")
    content_lists = discover_content_lists(batch_root)
    completed_roots = [
        (Path(record["output"]).resolve(), record["file"]) for record in completed
    ]
    packages: list[str] = []
    for path in content_lists:
        resolved = path.resolve()
        matched = [
            (root, filename)
            for root, filename in completed_roots
            if resolved == root or root in resolved.parents
        ]
        if len(matched) != 1:
            raise MinerUCloudError(f"无法把 MinerU Content List 对应回原始文件：{path}")
        _, original_filename = matched[0]
        packages.append(
            str(
                convert_content_list(
                    path,
                    package_root,
                    parser_version=f"mineru-cloud-{model_version}",
                    source_name=_source_title(original_filename),
                    source_filename=original_filename,
                )
            )
        )
    return {
        "status": "completed" if not failed else "partial",
        "batch_id": batch_id,
        "completed": completed,
        "failed": failed,
        "packages": packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="调用 MinerU 官方云服务解析文档，并转换为 mmwiki-0.1 Source Package"
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="一个或多个文件/目录")
    parser.add_argument("--output-root", type=Path, required=True, help="MinerU ZIP 解压目录")
    parser.add_argument("--package-root", type=Path, required=True, help="Source Package 输出目录")
    parser.add_argument("--model-version", choices=("pipeline", "vlm"), default="vlm")
    parser.add_argument("--language", default="ch")
    parser.add_argument("--ocr", action="store_true", help="强制 OCR；扫描 PDF 才建议开启")
    parser.add_argument("--page-ranges", default="", help="如 1-10；空表示整篇")
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args()

    config = _config()
    token = str(config.get("MINERU_API_TOKEN") or "").strip()
    if not token:
        raise MinerUCloudError(
            "缺少 MINERU_API_TOKEN；请在项目 .env 或进程环境变量中配置 MinerU 官网 Token"
        )
    api_base = str(config.get("MINERU_API_BASE_URL") or DEFAULT_API_BASE).strip()
    inputs = collect_inputs(args.inputs)
    result = parse_remote(
        inputs,
        args.output_root,
        args.package_root,
        token=token,
        api_base=api_base,
        model_version=args.model_version,
        language=args.language,
        enable_ocr=args.ocr,
        page_ranges=args.page_ranges,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MinerUCloudError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
