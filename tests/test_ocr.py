from __future__ import annotations

import unittest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from mmwiki.ocr import (
    QwenOCRProvider,
    build_ocr_payload,
    derive_ocr_url,
    extract_ocr_text,
)


class QwenOcrTests(unittest.TestCase):
    def test_native_endpoint_is_derived_from_compatible_base(self) -> None:
        self.assertEqual(
            derive_ocr_url(
                "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            ),
            "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        )

    def test_payload_uses_text_recognition_task(self) -> None:
        payload = build_ocr_payload(
            "qwen3.5-ocr", "data:image/png;base64,abc", "text_recognition", 3072, 8388608
        )
        image = payload["input"]["messages"][0]["content"][0]
        self.assertEqual(payload["model"], "qwen3.5-ocr")
        self.assertEqual(payload["parameters"]["ocr_options"]["task"], "text_recognition")
        self.assertEqual(image["image"], "data:image/png;base64,abc")
        self.assertEqual(image["min_pixels"], 3072)
        self.assertEqual(image["max_pixels"], 8388608)

    def test_text_is_extracted_from_native_response(self) -> None:
        self.assertEqual(
            extract_ocr_text(
                {
                    "output": {
                        "choices": [
                            {
                                "message": {
                                    "content": [
                                        {"text": "Layer 14"},
                                        {"text": "Recall@1 6.8%"},
                                    ]
                                }
                            }
                        ]
                    }
                }
            ),
            "Layer 14\nRecall@1 6.8%",
        )

    def test_provider_posts_native_request_with_bearer_header(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "output": {
                            "choices": [
                                {"message": {"content": [{"text": "6.8%"}]}}
                            ]
                        }
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "MMWIKI_API_BASE_URL": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                    "MMWIKI_API_KEY": "test-key",
                    "MMWIKI_OCR_API_URL": "",
                },
                clear=True,
            ):
                provider = QwenOCRProvider(Path(directory))
                with patch("mmwiki.ocr.urllib.request.urlopen", return_value=Response()) as post:
                    text, _ = provider.recognize("data:image/png;base64,abc")

        request = post.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(text, "6.8%")
        self.assertEqual(
            request.full_url,
            "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(body["parameters"]["ocr_options"]["task"], "text_recognition")


if __name__ == "__main__":
    unittest.main()
