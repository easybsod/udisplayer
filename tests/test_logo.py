import io
import random
import unittest

try:
    from PIL import Image
except ImportError:
    Image = None

from udisplayer.logo import MAX_LOGO_EDGE, optimize_png


@unittest.skipIf(Image is None, "Pillow not installed")
class LogoTests(unittest.TestCase):
    def test_large_png_is_resized_and_smaller(self):
        width, height = 640, 320
        pixels = random.Random(0).randbytes(width * height * 3)
        image = Image.frombytes("RGB", (width, height), pixels)
        source = io.BytesIO()
        image.save(source, format="PNG")
        original = source.getvalue()

        optimized = optimize_png(original)

        self.assertLess(len(optimized), len(original))
        with Image.open(io.BytesIO(optimized)) as result:
            self.assertLessEqual(max(result.size), MAX_LOGO_EDGE)
            self.assertEqual(result.size, (160, 80))

    def test_invalid_png_falls_back_unchanged(self):
        data = b"not-a-png"
        self.assertEqual(optimize_png(data), data)


if __name__ == "__main__":
    unittest.main()
