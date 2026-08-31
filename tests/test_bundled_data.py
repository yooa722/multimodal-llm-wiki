from __future__ import annotations

import json
import unittest
from pathlib import Path

from mmwiki.contracts import validate_package


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BundledDataTests(unittest.TestCase):
    def test_dataset_index_matches_valid_source_packages(self) -> None:
        data_root = PROJECT_ROOT / "data"
        index = json.loads((data_root / "index.json").read_text(encoding="utf-8"))
        packages = index["packages"]
        totals = {"packages": len(packages), "items": 0, "chunks": 0, "assets": 0}

        for record in packages:
            result = validate_package(data_root / record["path"])
            self.assertEqual(result["package_id"], record["package_id"])
            self.assertEqual(result["checksum"], record["checksum"])
            self.assertEqual(result["checksum"][:12], record["source_version"])
            for key in ("items", "chunks", "assets"):
                self.assertEqual(result["counts"][key], record[key])
                totals[key] += record[key]

        self.assertEqual(totals, index["totals"])

    def test_dataset_index_matches_bundled_demo_runtime(self) -> None:
        data_root = PROJECT_ROOT / "data"
        index = json.loads((data_root / "index.json").read_text(encoding="utf-8"))
        state = json.loads(
            (
                PROJECT_ROOT
                / "runtime/official-image-text/wiki-runtime/state.json"
            ).read_text(encoding="utf-8")
        )
        indexed_versions = {
            record["package_id"]: record["source_version"]
            for record in index["packages"]
        }
        runtime_versions = {
            source_id: source["source_version"]
            for source_id, source in state["sources"].items()
        }

        self.assertEqual(indexed_versions, runtime_versions)


if __name__ == "__main__":
    unittest.main()
