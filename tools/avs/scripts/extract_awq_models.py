#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
"""Extract a normative AVS+ model-selector table from pinned libcavs source.

The upstream BSD-3-Clause table is fetched during the local FFmpeg build,
not stored in this repository. This helper validates the expected structure
and writes only the C array initializer entries into the build directory.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def extract(source: str) -> str:
    expression = re.compile(
        r"static\s+const\s+uint8_t\s+model_selector\s*"
        r"\[\s*3\s*\]\s*\[\s*64\s*\]\s*=\s*\{(.*?)\n\};",
        re.DOTALL,
    )
    match = expression.search(source)
    if not match:
        raise ValueError("Upstream libcavs model_selector[3][64] not found")
    body = match.group(1)
    entries = re.findall(r"\{([^{}]+)\}", body)
    # Fail closed if parsing misses unexpected syntax/comments/extra data.
    remainder = re.sub(r"\{[0-9,\s]+\}", "", body)
    if len(entries) != 3 or re.fullmatch(r"[\s,]*", remainder) is None:
        raise ValueError("Unexpected libcavs model selector structure")
    out = []
    for entry in entries:
        if re.fullmatch(r"[0-9,\s]+", entry) is None:
            raise ValueError("Unexpected model selector values")
        values = [int(value) for value in re.findall(r"\d+", entry)]
        if len(values) != 64 or any(value > 5 for value in values):
            raise ValueError("Model selector must have 64 entries in 0..5")
        rows = [", ".join(map(str, values[i:i + 8])) for i in range(0, 64, 8)]
        out.append("                {\n" + "".join(
            f"                    {row},\n" for row in rows
        ).rstrip(",\n") + "\n                }")
    return ",\n".join(out) + "\n"


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("source", type=Path, help="pinned libcavs broadcast_quant.c")
    cli.add_argument("output", type=Path, help="FFmpeg build-only .inc file")
    args = cli.parse_args()
    initializer = extract(args.source.read_text(encoding="utf-8"))
    args.output.write_text(initializer, encoding="utf-8")
    print("Generated AVS+ AWQ model selectors from pinned upstream libcavs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
