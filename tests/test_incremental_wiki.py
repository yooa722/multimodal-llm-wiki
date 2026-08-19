from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import shutil

from mmwiki.contracts import load_package
from mmwiki.markdown_overlay import load_caption_map
from mmwiki.pipeline import WikiPipeline
from mmwiki.retrieval import RetrievalIndex


class IncrementalWikiTests(unittest.TestCase):
    def test_mineru_caption_is_matched_by_image_sha256(self) -> None:
        package_root = Path(__file__).parents[1] / "data/source_packages/论文_002_cs_LG/1d9dabf3e92b"
        package = load_package(package_root)
        item = next(item for item in package.items if item.asset_ids and item.caption)
        asset = package.assets[item.asset_ids[0]]
        caption_map = load_caption_map(package_root)

        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory) / "user-wiki"
            external.mkdir(parents=True)
            shutil.copy2(package_root / asset.path, external / Path(asset.path).name)
            (external / "page.md").write_text(
                f"![原始说明]({Path(asset.path).name})\n", encoding="utf-8"
            )
            pipeline = WikiPipeline(Path(directory) / "project")
            pipeline.ingest_existing_wiki(external, package_root)
            derived = (
                pipeline.vault / "wiki/external/user-wiki/pages/page.md"
            ).read_text(encoding="utf-8")

        self.assertIn(caption_map[asset.sha256]["caption"], derived)

    def test_existing_wiki_is_registered_in_text_flow_and_reimport_is_lightweight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            external = Path(directory) / "user-wiki"
            external.mkdir(parents=True)
            (external / "page.md").write_text(
                "# 架构说明\n\n![用户架构图](diagram.png)\n",
                encoding="utf-8",
            )
            (external / "diagram.png").write_bytes(b"diagram")
            original = (external / "page.md").read_bytes()
            pipeline = WikiPipeline(project)

            first = pipeline.ingest_existing_wiki(external)
            second = pipeline.ingest_existing_wiki(external)

            self.assertEqual(first["source_id"], "external-user-wiki")
            self.assertIn(first["source_id"], pipeline._load_state()["sources"])
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(second["assets_copied"], 0)
            self.assertEqual((external / "page.md").read_bytes(), original)
            self.assertEqual(pipeline.lint()["status"], "passed")

    def test_default_visual_request_does_not_call_vectors_and_preserves_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            external = Path(directory) / "user-wiki"
            external.mkdir(parents=True)
            (external / "page.md").write_text("# 架构\n\n系统架构说明\n", encoding="utf-8")
            pipeline = WikiPipeline(project)
            pipeline.ingest_existing_wiki(external)

            with patch.object(RetrievalIndex, "build", side_effect=AssertionError("must not build")):
                result = pipeline.build_retrieval_index()
            search = pipeline.search_with_trace("系统架构", retrieval_mode="multimodal")

            self.assertEqual(result["status"], "disabled")
            self.assertEqual(search["retrieval"]["mode"], "lexical")
            self.assertIn("向量检索已关闭", search["retrieval"]["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
