from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .pipeline import PipelineError, WikiPipeline
from .provider import OpenAICompatibleProvider, ProviderError


def serve(project_root: Path, host: str = "127.0.0.1", port: int = 19828) -> None:
    pipeline = WikiPipeline(project_root)

    def online_status() -> dict[str, Any]:
        provider = OpenAICompatibleProvider(project_root, "vision")
        return {
            "status": "ok" if provider.configured else "needs_configuration",
            "mode": "online_multimodal_qa",
            "configured": provider.configured,
            "model": provider.model or None,
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

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/v1/health":
                value = online_status()
                self._send(200 if value["configured"] else 503, value)
            elif self.path == "/api/v1/sources":
                self._send(200, {"sources": pipeline.sources()})
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
                        str(body.get("retrieval_mode") or "hybrid"),
                    )
                elif self.path == "/api/v1/search":
                    result = pipeline.search_with_trace(
                        str(body.get("question") or body.get("query") or ""),
                        int(body.get("top_k", 5)),
                        self._source_ids(body),
                        str(body.get("retrieval_mode") or "hybrid"),
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
