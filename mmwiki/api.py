from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .pipeline import PipelineError, WikiPipeline
from .provider import OpenAICompatibleProvider, ProviderError
from .web import render_query_html, render_wiki_html, resolve_vault_path


PRESENTATION_VERSION = "split-query-v2"


def serve(project_root: Path, host: str = "127.0.0.1", port: int = 19828) -> None:
    pipeline = WikiPipeline(project_root)
    vault_root = project_root / "runtime/vault"

    def online_status() -> dict[str, Any]:
        task = "vision" if pipeline.features.enable_vlm else "answer"
        provider = OpenAICompatibleProvider(project_root, task)
        return {
            "status": "ok" if provider.configured else "needs_configuration",
            "presentation_version": PRESENTATION_VERSION,
            "project_root": str(project_root.resolve()),
            "server_pid": os.getpid(),
            "mode": "online_multimodal_qa" if pipeline.features.enable_vlm else "online_text_qa",
            "configured": provider.configured,
            "model": provider.model or None,
            "feature_config": pipeline.features.as_dict(),
            "retrieval": pipeline.retrieval_status(),
        }

    class Handler(BaseHTTPRequestHandler):
        @staticmethod
        def _source_ids(body: dict[str, Any]) -> set[str] | None:
            values = body.get("source_ids", [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise PipelineError("source_ids 必须是字符串数组")
            return set(values) or None

        def _send(self, status: int, value: dict[str, Any]) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            request = urlsplit(self.path)
            if request.path == "/api/v1/health":
                value = online_status()
                self._send(200 if value["configured"] else 503, value)
            elif request.path == "/api/v1/sources":
                self._send(200, {"sources": pipeline.sources()})
            elif request.path == "/query/view":
                try:
                    values = parse_qs(request.query)
                    query_id = (values.get("id") or [""])[0]
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", query_id):
                        raise ValueError("Query ID 格式错误")
                    evidence = int((values.get("evidence") or ["1"])[0])
                    view = (values.get("view") or ["wiki"])[0]
                    state = pipeline._load_state()
                    record = next(
                        (
                            value
                            for value in reversed(state.get("queries", []))
                            if str(value.get("query_id") or "") == query_id
                        ),
                        None,
                    )
                    if record is None:
                        raise FileNotFoundError(query_id)
                    host = self.headers.get("Host", "127.0.0.1:19828")
                    payload = render_query_html(
                        record,
                        f"http://{host}",
                        evidence=evidence,
                        view=view,
                    )
                    self._send_bytes(200, payload, "text/html; charset=utf-8")
                except (ValueError, FileNotFoundError, OSError) as exc:
                    self._send(404, {"error": "query_not_found", "message": str(exc)})
            elif request.path.startswith("/api/v1/media/"):
                try:
                    relative = request.path.removeprefix("/api/v1/media/")
                    path = resolve_vault_path(
                        vault_root,
                        relative,
                        required_prefix=("assets", "wiki"),
                        allowed_suffixes={
                            ".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg",
                            ".png", ".svg", ".tif", ".tiff", ".webp",
                        },
                    )
                    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    self._send_bytes(200, path.read_bytes(), content_type)
                except (ValueError, FileNotFoundError, OSError) as exc:
                    self._send(404, {"error": "media_not_found", "message": str(exc)})
            elif request.path in {"/wiki/view", "/api/v1/wiki/raw"}:
                try:
                    values = parse_qs(request.query).get("path", [])
                    relative = values[0] if values else ""
                    path = resolve_vault_path(
                        vault_root,
                        relative,
                        required_prefix="wiki",
                        allowed_suffixes={".md"},
                    )
                    markdown = path.read_text(encoding="utf-8")
                    if request.path == "/wiki/view":
                        host = self.headers.get("Host", "127.0.0.1:19828")
                        payload = render_wiki_html(markdown, relative, f"http://{host}")
                        self._send_bytes(200, payload, "text/html; charset=utf-8")
                    else:
                        self._send_bytes(
                            200,
                            markdown.encode("utf-8"),
                            "text/markdown; charset=utf-8",
                        )
                except (ValueError, FileNotFoundError, OSError, UnicodeDecodeError) as exc:
                    self._send(404, {"error": "wiki_not_found", "message": str(exc)})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1024 * 1024:
                    raise PipelineError("请求体不能超过 1 MiB")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if not isinstance(body, dict):
                    raise PipelineError("请求体必须是 JSON 对象")
                if self.path == "/api/v1/query":
                    if not online_status()["configured"]:
                        raise ProviderError("在线问答未配置，请检查项目 .env")
                    result = pipeline.query(
                        str(body.get("question") or ""),
                        int(body.get("top_k", 5)),
                        "api",
                        self._source_ids(body),
                        str(body.get("retrieval_mode") or "auto"),
                    )
                elif self.path == "/api/v1/search":
                    result = pipeline.search_with_trace(
                        str(body.get("question") or body.get("query") or ""),
                        int(body.get("top_k", 5)),
                        self._source_ids(body),
                        str(body.get("retrieval_mode") or "auto"),
                    )
                else:
                    self._send(404, {"error": "not_found"})
                    return
                self._send(200, result)
            except (ValueError, PipelineError, ProviderError) as exc:
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid_json"})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Multimodal Wiki API: http://{host}:{port}")
    server.serve_forever()
