from __future__ import annotations

import unittest

from tools.mineru_reference_to_package import is_searchable_item


class MineruAdapterTests(unittest.TestCase):
    def test_uncaptioned_visual_asset_remains_searchable(self) -> None:
        self.assertTrue(is_searchable_item("image", "", ["asset-1"]))
        self.assertTrue(is_searchable_item("chart", "", ["asset-1"]))

    def test_empty_non_visual_item_is_not_searchable(self) -> None:
        self.assertFalse(is_searchable_item("paragraph", "", []))


if __name__ == "__main__":
    unittest.main()
