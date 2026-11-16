#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 udisplayer contributors
# SPDX-License-Identifier: MIT
"""Read-only MPEG-TS continuity/PES timestamp analyzer for local test captures.

Does not connect to networks, emit addresses, or alter the IPTV service.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


TS_PACKET_SIZE = 188
PTS_MODULUS = 1 << 33


def signed_clock_delta(a: int, b: int) -> int:
    """Wrap-safe signed difference for MPEG-TS 90 kHz, 33-bit clock."""
    return ((a - b + PTS_MODULUS // 2) % PTS_MODULUS) - PTS_MODULUS // 2


def decode_pts(five: bytes) -> int | None:
    if len(five) != 5 or any(not five[i] & 1 for i in (0, 2, 4)):
        return None
    return ((five[0] >> 1 & 0x07) << 30 |
            five[1] << 22 | (five[2] >> 1) << 15 |
            five[3] << 7 | five[4] >> 1)


def pes_timestamps(payload: bytes) -> tuple[int | None, int | None]:
    if len(payload) < 9 or payload[:3] != b"\x00\x00\x01":
        return None, None
    # Header is present only in elementary streams with optional PES flags.
    if payload[6] & 0xC0 != 0x80:
        return None, None
    flags = (payload[7] >> 6) & 3
    length = payload[8]
    if 9 + length > len(payload):
        # The timestamp may straddle packets; do not guess.
        return None, None
    if flags not in (2, 3) or length < 5:
        return None, None
    pts = decode_pts(payload[9:14])
    if pts is None or payload[9] >> 4 != flags:
        return None, None
    dts = None
    if flags == 3:
        if length < 10 or payload[14] >> 4 != 1:
            return pts, None
        dts = decode_pts(payload[14:19])
    return pts, dts


def analyze(path: Path) -> dict:
    counters: collections.Counter[str] = collections.Counter()
    per_pid: dict[int, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    last_cc: dict[int, int] = {}
    last_dts: dict[int, int] = {}

    with path.open("rb") as source:
        while packet := source.read(TS_PACKET_SIZE):
            if len(packet) < TS_PACKET_SIZE:
                counters["truncated_tail_bytes"] += len(packet)
                break
            counters["packets"] += 1
            if packet[0] != 0x47:
                counters["sync_errors"] += 1
                # This checker expects 188-byte aligned TS; after a sync loss
                # its CC results may also be invalid until alignment is fixed.
                continue
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            if pid == 0x1FFF:  # null stuffing packets
                counters["null_packets"] += 1
                continue
            metrics = per_pid[pid]
            metrics["packets"] += 1
            if packet[1] & 0x80:
                metrics["transport_error_indicator"] += 1
                last_cc.pop(pid, None)
                last_dts.pop(pid, None)
                continue
            control = packet[3] >> 4 & 3
            cc = packet[3] & 15
            if control == 0:
                metrics["invalid_adaptation_control"] += 1
                continue
            payload = bool(control & 1)
            has_adaptation = bool(control & 2)
            offset = 4
            discontinuity = False
            if has_adaptation:
                adaptation_length = packet[offset]
                offset += adaptation_length + 1
                if offset > TS_PACKET_SIZE:
                    metrics["bad_adaptation_length"] += 1
                    continue
                if adaptation_length:
                    discontinuity = bool(packet[5] & 0x80)
                    if discontinuity:
                        metrics["signaled_discontinuities"] += 1
                        last_cc.pop(pid, None)
                        last_dts.pop(pid, None)
            if payload and offset < TS_PACKET_SIZE:
                old = last_cc.get(pid)
                if old is not None and not discontinuity:
                    if cc == old:
                        metrics["repeated_cc"] += 1
                    elif cc != (old + 1) % 16:
                        metrics["continuity_errors"] += 1
                last_cc[pid] = cc
                metrics["payload_packets"] += 1
                if packet[1] & 0x40:  # payload_unit_start
                    pts, dts = pes_timestamps(packet[offset:])
                    if pts is not None:
                        metrics["pes_pts"] += 1
                    if dts is not None:
                        metrics["pes_dts"] += 1
                        if signed_clock_delta(pts, dts) < 0:
                            metrics["pts_before_dts"] += 1
                        prev = last_dts.get(pid)
                        if prev is not None and signed_clock_delta(dts, prev) < 0:
                            metrics["dts_regressions"] += 1
                        last_dts[pid] = dts
            elif not payload and pid in last_cc and cc != last_cc[pid]:
                # Adaptation-only packets must preserve the last CC.
                metrics["adaptation_only_cc_changes"] += 1

    return {
        "packet_size": TS_PACKET_SIZE,
        "summary": dict(counters),
        "pids": {f"0x{pid:04x}": dict(counts)
                 for pid, counts in sorted(per_pid.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 MPEG-TS 188-byte 連續計數與 PES 時間戳檢查")
    parser.add_argument("input", type=Path, help="合法取得的本地 MPEG-TS 錄製檔；不會連線網路")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error("找不到輸入檔")
    print(json.dumps(analyze(args.input), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
