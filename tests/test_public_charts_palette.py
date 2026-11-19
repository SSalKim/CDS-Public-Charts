import io
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from public_charts import policy
from public_charts.producer import encode, palette_quantize

KEY = np.array([65536, 256, 1])


def chart_like(width=600, height=400, seed=7):
    """Flat shading levels and lines, anti-aliased like a resized matplotlib frame."""
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", (width * 2, height * 2), "white")
    draw = ImageDraw.Draw(image)
    levels = [(int(r), int(g), int(b)) for r, g, b in rng.integers(0, 256, (40, 3))]
    for index, colour in enumerate(levels):
        x, y = rng.integers(0, width * 2, 2)
        draw.ellipse((x - 160, y - 110, x + 160 + index, y + 110), fill=colour)
    for index in range(120):
        points = [tuple(int(v) for v in rng.integers(0, width * 2, 2)) for _ in range(4)]
        draw.line(points, fill=levels[index % len(levels)], width=3)
    for index in range(60):
        x, y = rng.integers(0, width * 2 - 40, 2)
        draw.text((int(x), int(y)), str(500 + index), fill=(200, 0, 0))
    return image.filter(ImageFilter.GaussianBlur(0.8)).resize((width, height), Image.Resampling.LANCZOS)


def colour_counts(image):
    keys = np.asarray(image).reshape(-1, 3).astype(np.int64) @ KEY
    return np.unique(keys, return_inverse=True, return_counts=True)


def webp_size(image):
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", lossless=True, quality=80, method=4)
    return buffer.tell()


class PaletteQuantizeTests(unittest.TestCase):
    def setUp(self):
        self.frame = chart_like()
        self.assertGreater(len(colour_counts(self.frame)[0]), 256, "fixture must need quantization")

    def test_dominant_colours_are_exact_and_output_fits_a_palette(self):
        result = palette_quantize(self.frame)
        before = np.asarray(self.frame).astype(int)
        after = np.asarray(result).astype(int)
        self.assertEqual(result.size, self.frame.size)
        self.assertLessEqual(len(colour_counts(result)[0]), 256)
        unique, inverse, counts = colour_counts(self.frame)
        error = np.abs(before - after).max(2).reshape(-1)
        worst = np.zeros(len(unique))
        np.maximum.at(worst, inverse, error)
        dominant = np.flatnonzero(counts >= 0.001 * len(inverse))
        self.assertTrue((worst[dominant] == 0).all(), "Shading, background and line colours must not change")
        self.assertLess(error.max(), 80)

    def test_quantized_chart_is_smaller_and_deterministic(self):
        result = palette_quantize(self.frame)
        self.assertLess(webp_size(result), webp_size(self.frame))
        np.testing.assert_array_equal(np.asarray(result), np.asarray(palette_quantize(self.frame)))

    def test_charts_that_already_fit_are_unchanged(self):
        small = self.frame.quantize(200, method=Image.Quantize.MAXCOVERAGE).convert("RGB")
        np.testing.assert_array_equal(np.asarray(palette_quantize(small)), np.asarray(small))

    def test_encode_stores_the_quantized_pixels_losslessly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chart.webp"
            encode(self.frame, path, palette=True)
            with Image.open(path) as decoded:
                stored = np.asarray(decoded.convert("RGB"))
        np.testing.assert_array_equal(stored, np.asarray(palette_quantize(self.frame)))

    def test_encode_without_palette_is_still_pixel_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chart.webp"
            encode(self.frame, path)
            with Image.open(path) as decoded:
                np.testing.assert_array_equal(np.asarray(decoded.convert("RGB")), np.asarray(self.frame))


class PaletteCutoverTests(unittest.TestCase):
    def test_only_cycles_from_the_cutover_use_the_palette(self):
        self.assertFalse(policy.palette_enabled("2026111906"))
        self.assertTrue(policy.palette_enabled(policy.PALETTE_CUTOVER))

    def test_existing_cycles_keep_their_render_versions(self):
        # Older rows must stay current so retained charts are not re-rendered.
        for model, product, domain in (("gfs", "gph500", "asia"), ("ukmo", "acptot", "hkor"),
                                       ("cmc", "acrain", "hkor"), ("ukmo", "gph500", "asia")):
            old = policy._layout_render_version("2026111906", model, product, domain)
            self.assertEqual(policy.render_version("2026111906", model, product, domain), old)
            row = {"cycle": "2026111906", "model": model, "product": product, "domain": domain, "render_version": old}
            self.assertTrue(policy.render_is_current(row))

    def test_new_cycles_get_a_distinct_render_version(self):
        cycle = policy.PALETTE_CUTOVER
        for model, product, domain in (("gfs", "gph500", "asia"), ("cmc", "acrain", "hkor")):
            layout = policy._layout_render_version(cycle, model, product, domain)
            self.assertEqual(policy.render_version(cycle, model, product, domain), layout + policy.PALETTE_RENDER_STEP)
            stale = {"cycle": cycle, "model": model, "product": product, "domain": domain, "render_version": layout}
            self.assertFalse(policy.render_is_current(stale), "A pre-palette image of a new cycle is redrawn")


if __name__ == "__main__":
    unittest.main()
