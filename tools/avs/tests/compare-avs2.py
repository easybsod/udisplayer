#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
"""Compare complete visible Y/U/V frame hashes: full AVS2 decode vs opt-in skip.

Use only valid raw AVS2 byte streams (.avs), not MPEG-TS. Prefer a sample cut
at a sequence/GOP boundary; a partial trailing picture can decode differently.
This checker works with the pinned patched davs2 CLI built by build-davs2-10bit.sh.
"""
import argparse
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time

FULL_HASH = re.compile(r"UDISPLAYER_AVS2_FULL_HASH poc=(\d+) type=(\d+) hash=([0-9a-f]+)")


def run(decoder: Path, source: Path, threads: int, skipping: bool, timeout: int):
    env = os.environ.copy()
    env["UDISPLAYER_AVS2_FULL_HASH"] = "1"
    env.pop("UDISPLAYER_AVS2_SKIP_NONREF", None)
    if skipping:
        env["UDISPLAYER_AVS2_SKIP_NONREF"] = "1"
    command = [str(decoder), "-i", str(source), "-o", os.devnull, "-t", str(threads)]
    started = time.monotonic()
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)
    elapsed = time.monotonic() - started
    output = result.stdout + result.stderr
    hashes = {int(poc): (int(pictype), value) for poc, pictype, value in FULL_HASH.findall(output)}
    if result.returncode or not hashes:
        raise RuntimeError(f"Decoder failed (skip={skipping}, rc={result.returncode}): {output[-700:]}")
    return elapsed, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decoder", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3,
                        help="Repeat the optimized decode to detect concurrency-dependent mismatches")
    parser.add_argument("--trim-last-gop", action="store_true",
                        help="Drop the last sequence beginning at 00 00 01 b0 (avoids partial EOF picture)")
    args = parser.parse_args()
    if args.threads < 1 or args.timeout < 1 or not 1 <= args.repeats <= 10:
        parser.error("threads and timeout must be positive; repeats must be 1..10")
    if not args.decoder.is_file() or not args.input.is_file():
        parser.error("decoder and input must be existing files")
    with tempfile.TemporaryDirectory(prefix="avs2-hash-check-") as temporary:
        source = args.input
        if args.trim_last_gop:
            data = source.read_bytes()
            marker = b"\x00\x00\x01\xb0"
            last = data.rfind(marker)
            if last <= 0 or data.find(marker) >= last:
                parser.error("need at least two sequence headers to trim the final GOP")
            source = Path(temporary) / "trim.avs"
            source.write_bytes(data[:last])
        original_time, original = run(args.decoder, source, args.threads, False, args.timeout)
        optimized_runs = [run(args.decoder, source, args.threads, True, args.timeout)
                          for _ in range(args.repeats)]
    fast_time = statistics.median(elapsed for elapsed, _ in optimized_runs)
    fast = optimized_runs[0][1]
    missing = sorted(set(fast) - set(original))
    changed = sorted(k for k in fast.keys() & original.keys() if fast[k] != original[k])
    unstable = sorted({poc for _, run_hashes in optimized_runs[1:]
                       for poc in fast.keys() | run_hashes.keys()
                       if fast.get(poc) != run_hashes.get(poc)})
    print(f"Full: {len(original)} frames in {original_time:.2f}s")
    print(f"Skip: {len(fast)} frames; median {fast_time:.2f}s over {args.repeats} run(s)")
    print(f"Preserved full-plane hashes: {len(fast) - len(changed) - len(missing)}/{len(fast)}")
    print(f"Time reduction: {(1 - fast_time / original_time) * 100:.1f}%")
    if missing or changed or unstable:
        print(f"ERROR mismatched POC: missing={missing[:10]} altered={changed[:10]} "
              f"unstable_across_runs={unstable[:10]}", file=sys.stderr)
        return 1
    if len(fast) >= len(original):
        print("ERROR skip had no effect: verify 10-bit 50/60fps sample and build", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
