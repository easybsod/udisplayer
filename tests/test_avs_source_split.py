# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
"""Ensure third-party AVS patches are retrieved rather than vendored."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/avs/scripts/extract_awq_models.py"
SPEC = importlib.util.spec_from_file_location("extract_awq_models", SCRIPT)
assert SPEC and SPEC.loader
EXTRACTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACTOR)


class AVSSourceSplitTests(unittest.TestCase):
    def test_extractor_validates_exact_matrix_shape(self):
        row = ",".join(str(i % 6) for i in range(64))
        template = "static const uint8_t model_selector[3][64] = {\n"
        template += ",\n".join("{" + row + "}" for _ in range(3)) + "\n};"
        result = EXTRACTOR.extract(template)
        self.assertEqual(result.count("{"), 3)
        self.assertEqual(result.count("}"), 3)
        self.assertIn("0, 1, 2, 3", result)
        with self.assertRaises(ValueError):
            EXTRACTOR.extract(template.replace("model_selector", "untrusted_data"))
        with self.assertRaises(ValueError):
            EXTRACTOR.extract(template.replace(row, "9,9,9"))

    def test_public_source_contains_only_downstream_davs2_patch(self):
        files = sorted((ROOT / "tools/avs/patches/davs2").glob("*.patch"))
        self.assertEqual([p.name for p in files], ["main10-udisplayer.patch"])
        downstream = files[0].read_text(encoding="utf-8")
        self.assertNotIn("intrinsic_inter_pred_avx2.cc", downstream)
        self.assertNotIn("diff --git a/source/common/mc.cc", downstream)
        self.assertIn("diff --git a/source/common/sao.cc", downstream)
        self.assertIn("diff --git a/source/common/header.cc", downstream)

    def test_third_party_pins_and_build_order(self):
        manifest = (ROOT / "tools/avs/manifests/davs2-upstream-patches.sha256").read_text()
        build = (ROOT / "tools/avs/scripts/build-davs2-10bit.sh").read_text()
        for number in ("0001-", "0012-", "0013-"):
            self.assertIn(number, manifest)
            self.assertIn(number, build)
        self.assertIn("--unidiff-zero", build)
        self.assertIn("NILAODA_COMMIT", build)
        self.assertNotIn("main10-experimental.patch", build)

    def test_ffmpeg_table_is_generated_not_vendored(self):
        patch = (ROOT / "tools/avs/patches/ffmpeg/avsplus-awq.patch").read_text()
        build = (ROOT / "tools/avs/scripts/build-ffmpeg.sh").read_text()
        self.assertIn("cavs_awq_models.inc", patch)
        self.assertIn("extract_awq_models.py", build)
        self.assertIn("LIBCAVS_COMMIT", build)


if __name__ == "__main__":
    unittest.main()
