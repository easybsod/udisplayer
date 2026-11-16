import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools/avs/scripts/analyze_mpegts.py"
spec = importlib.util.spec_from_file_location("analyze_avs_mpegts", SCRIPT)
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


def packet(pid: int, cc: int, *, adaptation: bool = False,
           discontinuity: bool = False, payload: bytes = b"\xff") -> bytes:
    start = b"\x47" + bytes([((pid >> 8) & 31) | (0x40 if payload.startswith(b"\0\0\1") else 0),
                            pid & 0xFF])
    if adaptation:
        data = bytes([1, 0x80 if discontinuity else 0])
        header = start + bytes([0x30 | cc]) + data
    else:
        header = start + bytes([0x10 | cc])
    return (header + payload).ljust(188, b"\xff")


def stamp(value: int, prefix: int) -> bytes:
    return bytes([(prefix << 4) | (((value >> 30) & 7) << 1) | 1,
                  (value >> 22) & 255, ((value >> 15) & 127) << 1 | 1,
                  (value >> 7) & 255, (value & 127) << 1 | 1])


def pes(pts: int, dts: int) -> bytes:
    return b"\0\0\1\xe0\0\0\x80\xc0\x0a" + stamp(pts, 3) + stamp(dts, 1)


class TransportAnalyzerTests(unittest.TestCase):
    def check(self, content: bytes):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.ts"
            path.write_bytes(content)
            return ts.analyze(path)

    def test_continuity_discontinuity_and_adaptation(self):
        data = (packet(256, 14) + packet(256, 15) + packet(256, 0) +
                packet(256, 3) + packet(256, 4, adaptation=True,
                                         discontinuity=True) +
                packet(256, 5) + packet(8191, 0))
        result = self.check(data)
        p = result["pids"]["0x0100"]
        self.assertEqual(p["continuity_errors"], 1)
        self.assertEqual(p["signaled_discontinuities"], 1)
        self.assertEqual(result["summary"]["null_packets"], 1)
        self.assertEqual(result["summary"]["packets"], 7)

    def test_pts_dts_wrap_and_regression(self):
        data = (packet(501, 0, payload=pes((1 << 33) - 100, (1 << 33) - 300)) +
                packet(501, 1, payload=pes(200, 100)) +
                packet(501, 2, payload=pes(150, 50)) +
                packet(501, 3, payload=pes(150, 200)))
        p = self.check(data)["pids"]["0x01f5"]
        self.assertEqual(p["pes_pts"], 4)
        self.assertEqual(p["pes_dts"], 4)
        self.assertEqual(p["dts_regressions"], 1)
        self.assertEqual(p["pts_before_dts"], 1)

    def test_truncated_tail_and_transport_indicator(self):
        broken = bytearray(packet(123, 1))
        broken[1] |= 0x80
        p = self.check(bytes(broken) + packet(123, 0) + b"\x47\x01")
        self.assertEqual(p["pids"]["0x007b"]["transport_error_indicator"], 1)
        self.assertEqual(p["summary"]["truncated_tail_bytes"], 2)


if __name__ == "__main__":
    unittest.main()
