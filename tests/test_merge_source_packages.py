from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mmwiki.contracts import load_package
from tools.merge_source_packages import merge_packages
from tools.mineru_to_package import convert_content_list


class MergeSourcePackagesTests(unittest.TestCase):
    def test_page_split_packages_merge_into_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parts = []
            for number, text in ((1, "前半部分"), (2, "后半部分")):
                source = root / f"source-{number}"
                source.mkdir()
                path = source / f"part-{number}_content_list_v2.json"
                path.write_text(
                    json.dumps(
                        [[{"type": "paragraph", "content": {"paragraph_content": text}}]],
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                parts.append(convert_content_list(path, root / f"packages-{number}"))

            merged = merge_packages(
                parts,
                [0, 2],
                root / "merged",
                source_name="完整文档",
                source_filename="完整文档.pdf",
            )
            package = load_package(merged)

            self.assertEqual(package.package_id, "完整文档")
            self.assertEqual([item.page_start for item in package.items], [1, 3])
            self.assertEqual(
                [item.item_id for item in package.items],
                ["item-p0001-b0001", "item-p0003-b0001"],
            )
            self.assertEqual(package.items[0].metadata["relations"]["next_item_id"], "item-p0003-b0001")
            self.assertEqual(package.items[1].provenance["source_part"], 2)


if __name__ == "__main__":
    unittest.main()
