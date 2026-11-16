"""Keep AVS documentation free of machine-local endpoints.

Only generic documentation is checked here. Test fixture URLs in unrelated
functional tests do not contain private channel information.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "docs/avs.md",
    ROOT / "tools/avs/README.md",
    *(ROOT / "tools/avs/docs").glob("*.md"),
]
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
PERSONAL_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/")
REAL_CHANNEL_NAME = re.compile(r"[\u4e00-\u9fff]+(?:衛視|卫视|綜藝|综艺|電視台|电视台)")


class AVSDocumentPrivacyTests(unittest.TestCase):
    def test_avs_docs_exclude_private_hosts_and_channel_names(self):
        for path in DOCS:
            with self.subTest(file=path.relative_to(ROOT)):
                content = path.read_text(encoding="utf-8")
                self.assertIsNone(IPV4.search(content))
                self.assertIsNone(PERSONAL_PATH.search(content))
                self.assertIsNone(REAL_CHANNEL_NAME.search(content))


if __name__ == "__main__":
    unittest.main()
