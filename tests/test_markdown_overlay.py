from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mmwiki.markdown_overlay import materialize_wiki


class MarkdownOverlayTests(unittest.TestCase):
    def test_materialize_uses_caption_without_modifying_original_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wiki"
            output = Path(directory) / "derived"
            image = root / "images" / "figure.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"same-image")
            original = (
                "# 原始页面\n\n"
                "![旧说明](images/figure.png)\n\n"
                "![[images/figure.png]]\n"
            )
            page = root / "readme.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(original, encoding="utf-8")
            digest = hashlib.sha256(image.read_bytes()).hexdigest()

            result = materialize_wiki(
                root,
                output,
                {
                    digest: {
                        "asset_id": "asset-figure",
                        "caption": "展示系统总体架构",
                    }
                },
            )

            self.assertEqual(page.read_text(encoding="utf-8"), original)
            derived = (output / "pages" / "readme.md").read_text(encoding="utf-8")
            self.assertEqual(derived.count("![展示系统总体架构](../assets/asset-figure.png)"), 2)
            self.assertTrue((output / "assets" / "asset-figure.png").is_file())
            self.assertEqual(result["assets_copied"], 1)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["assets"][0]["caption_provenance"], "mineru")

    def test_missing_caption_preserves_existing_alt_and_reports_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wiki"
            output = Path(directory) / "derived"
            image = root / "image.jpg"
            root.mkdir(parents=True)
            image.write_bytes(b"uncaptioned")
            (root / "page.md").write_text(
                "![用户说明](image.jpg)\n![[image.jpg]]\n", encoding="utf-8"
            )

            materialize_wiki(root, output, {})

            derived = (output / "pages" / "page.md").read_text(encoding="utf-8")
            self.assertIn("![用户说明](../assets/asset-", derived)
            self.assertIn("![](../assets/asset-", derived)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["assets"][0]["caption_status"], "caption_missing")

    def test_invalid_or_remote_images_are_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "wiki"
            output = Path(directory) / "derived"
            root.mkdir(parents=True)
            (root / "page.md").write_text(
                "![outside](../secret.png)\n![remote](https://example.com/a.png)\n",
                encoding="utf-8",
            )

            result = materialize_wiki(root, output, {})

            self.assertEqual(result["assets_copied"], 0)
            self.assertGreaterEqual(len(result["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
