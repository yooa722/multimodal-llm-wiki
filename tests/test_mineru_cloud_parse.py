from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mmwiki.contracts import load_package
from tools.mineru_cloud_parse import (
    MAX_FILE_BYTES,
    MinerUCloudError,
    _source_title,
    collect_inputs,
    parse_remote,
    safe_extract_zip,
)


class MinerUCloudParseTests(unittest.TestCase):
    def test_source_title_removes_repeated_extensions(self) -> None:
        self.assertEqual(_source_title("厚叶卷瓣兰.pdf.pdf"), "厚叶卷瓣兰")

    def test_collect_inputs_filters_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "demo.pdf"
            pdf.write_bytes(b"pdf")
            (root / "ignore.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual(collect_inputs([root]), [pdf.resolve()])

    def test_collect_inputs_rejects_oversized_file_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large.pdf"
            with path.open("wb") as stream:
                stream.truncate(MAX_FILE_BYTES + 1)
            with self.assertRaisesRegex(MinerUCloudError, "200MB"):
                collect_inputs([path])

    def test_safe_extract_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.txt", "bad")
            with self.assertRaisesRegex(MinerUCloudError, "路径逃逸"):
                safe_extract_zip(archive, root / "output")
            self.assertFalse((root / "escape.txt").exists())

    def test_remote_flow_downloads_zip_and_builds_valid_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "demo.pdf"
            source.write_bytes(b"pdf")
            fake_zip = root / "result.zip"
            content_list = [
                {
                    "type": "text",
                    "text": "图 1 展示云端解析结果。",
                    "page_idx": 0,
                    "bbox": [10, 20, 900, 100],
                },
                {
                    "type": "image",
                    "img_path": "images/figure.png",
                    "image_caption": ["图 1 云端流程"],
                    "page_idx": 0,
                    "bbox": [10, 120, 900, 800],
                },
            ]
            with zipfile.ZipFile(fake_zip, "w") as bundle:
                bundle.writestr(
                    "demo_content_list.json",
                    json.dumps(content_list, ensure_ascii=False),
                )
                bundle.writestr("images/figure.png", b"image")

            def fake_download(_url: str, target: Path, **_kwargs: object) -> None:
                target.write_bytes(fake_zip.read_bytes())

            with (
                patch(
                    "tools.mineru_cloud_parse.submit_batch",
                    return_value=("batch-test", ["https://upload.example/demo"]),
                ),
                patch("tools.mineru_cloud_parse._put_file"),
                patch(
                    "tools.mineru_cloud_parse.wait_for_batch",
                    return_value=[
                        {
                            "file_name": "demo.pdf",
                            "state": "done",
                            "full_zip_url": "https://download.example/result.zip",
                        }
                    ],
                ),
                patch("tools.mineru_cloud_parse._download", side_effect=fake_download),
            ):
                result = parse_remote(
                    [source],
                    root / "mineru-output",
                    root / "packages",
                    token="secret-not-written",
                )

            self.assertEqual(result["status"], "completed")
            self.assertNotIn("secret-not-written", json.dumps(result))
            package = load_package(Path(result["packages"][0]))
            self.assertEqual(package.package_id, "demo")
            self.assertEqual(package.title, "demo")
            self.assertEqual(len(package.items), 2)
            self.assertEqual(len(package.assets), 1)
            self.assertEqual(package.items[1].page_start, 1)
            self.assertIn("云端解析结果", package.items[1].semantic["adjacent_text"])


if __name__ == "__main__":
    unittest.main()
