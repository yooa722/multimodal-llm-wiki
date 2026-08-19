from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mmwiki.config import FeatureConfig, load_feature_config, resolve_query_mode


class FeatureConfigTests(unittest.TestCase):
    def test_defaults_disable_vlm_and_vector_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                config = load_feature_config(Path(directory))
        self.assertEqual(config, FeatureConfig(enable_vlm=False, enable_vector_retrieval=False))

    def test_cli_overrides_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "MMWIKI_ENABLE_VLM=false\nMMWIKI_ENABLE_VECTOR_RETRIEVAL=false\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_feature_config(root, vlm="on", vector_retrieval="on")
        self.assertTrue(config.enable_vlm)
        self.assertTrue(config.enable_vector_retrieval)

    def test_auto_mode_uses_caption_lexical_search_by_default(self) -> None:
        config = FeatureConfig()
        self.assertEqual(resolve_query_mode("auto", False, config), "lexical")
        self.assertEqual(resolve_query_mode("multimodal", True, config), "lexical")

    def test_auto_mode_enables_hybrid_or_multimodal_only_when_configured(self) -> None:
        vectors = FeatureConfig(enable_vector_retrieval=True)
        self.assertEqual(resolve_query_mode("auto", False, vectors), "hybrid")
        self.assertEqual(
            resolve_query_mode(
                "auto", True, FeatureConfig(enable_vlm=True, enable_vector_retrieval=True)
            ),
            "multimodal",
        )


if __name__ == "__main__":
    unittest.main()
