"""Regression checks; synthetic cases do not establish real-device accuracy."""

import hashlib
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from crop_screenshot import RESAMPLE, detect_crop, open_image, run_pythonista


class BorderTests(unittest.TestCase):
    def scene(self, background, box=(30, 80, 270, 520), size=(300, 600)):
        image = Image.new("RGB", size, background)
        draw = ImageDraw.Draw(image)
        draw.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=(90, 140, 185))
        return image

    def assert_box_near(self, actual, expected, tolerance=4):
        for got, want in zip(actual, expected):
            self.assertLessEqual(abs(got - want), tolerance, (actual, expected))

    def test_black_and_white_four_sides(self):
        for color in ((0, 0, 0), (255, 255, 255), (12, 15, 19), (241, 243, 245)):
            with self.subTest(color=color):
                image = self.scene(color)
                result = detect_crop(image)
                self.assertEqual(result.status, "candidate")
                self.assert_box_near(result.box, (30, 80, 270, 520))

    def test_top_and_bottom_only(self):
        result = detect_crop(self.scene("black", (0, 80, 300, 520)))
        self.assert_box_near(result.box, (0, 80, 300, 520))

    def test_left_and_right_only(self):
        result = detect_crop(self.scene("white", (40, 0, 260, 600)))
        self.assert_box_near(result.box, (40, 0, 260, 600))

    def test_landscape(self):
        image = self.scene("black", (80, 30, 520, 270), (600, 300))
        self.assert_box_near(detect_crop(image).box, (80, 30, 520, 270))

    def test_toolbar_icons_do_not_become_content(self):
        image = self.scene("black", (0, 90, 300, 480))
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 35, 22), fill="white")
        draw.rectangle((100, 550, 160, 570), fill="white")
        self.assert_box_near(detect_crop(image).box, (0, 90, 300, 480))

    def test_dark_area_inside_photo_is_preserved(self):
        image = self.scene("black", (0, 90, 300, 480))
        ImageDraw.Draw(image).rectangle((15, 120, 150, 450), fill="black")
        self.assert_box_near(detect_crop(image).box, (0, 90, 300, 480))

    def test_white_area_inside_photo_is_preserved(self):
        image = self.scene("white", (0, 90, 300, 480))
        ImageDraw.Draw(image).rectangle((15, 120, 150, 450), fill="white")
        self.assert_box_near(detect_crop(image).box, (0, 90, 300, 480))

    def test_two_images_require_review(self):
        image = Image.new("RGB", (300, 600), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 80, 299, 240), fill="orange")
        draw.rectangle((0, 340, 299, 500), fill="cyan")
        result = detect_crop(image)
        self.assertEqual(result.status, "review")
        self.assertEqual(result.box, (0, 0, 300, 600))

    def test_no_border_or_all_blank(self):
        for color in ("black", "white", "orange"):
            with self.subTest(color=color):
                result = detect_crop(Image.new("RGB", (300, 600), color))
                self.assertEqual(result.status, "review")
                self.assertEqual(result.box, (0, 0, 300, 600))

    def test_jpeg_compression(self):
        buffer = io.BytesIO()
        self.scene("white").save(buffer, format="JPEG", quality=55)
        buffer.seek(0)
        with Image.open(buffer) as image:
            self.assert_box_near(detect_crop(image).box, (30, 80, 270, 520))

    def test_resolution_changes(self):
        image = self.scene("black")
        for factor in (0.5, 1.0, 2.0, 4.0):
            with self.subTest(scale=factor):
                scaled = image.resize((round(image.width * factor), round(image.height * factor)), RESAMPLE)
                box = detect_crop(scaled).box
                normalized = tuple(value / factor for value in box)
                self.assert_box_near(normalized, (30, 80, 270, 520))

    def test_tiny_image_does_not_crash(self):
        result = detect_crop(Image.new("RGB", (1, 1), "white"))
        self.assertEqual(result.status, "review")


class ProvidedSampleTests(unittest.TestCase):
    sample_dir = Path(__file__).parent / "sample"

    def test_results_are_crops_of_original_pixels(self):
        paths = sorted(self.sample_dir.glob("*.jpg"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(file=path.name):
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                image = open_image(path)
                result = detect_crop(image)
                self.assertEqual(result.status, "candidate")
                self.assertEqual(result.box[0], 0)
                self.assertEqual(result.box[2], image.width)
                crop = image.crop(result.box)
                self.assertGreater(crop.height, image.height * 0.5)
                self.assertLess(crop.height, image.height * 0.8)
                self.assertEqual(crop.getpixel((0, 0)), image.getpixel(result.box[:2]))
                self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_sample_scaling_consistency(self):
        for path in sorted(self.sample_dir.glob("*.jpg")):
            image = open_image(path)
            original = detect_crop(image)
            normalized = tuple(value / (image.width if i % 2 == 0 else image.height)
                               for i, value in enumerate(original.box))
            for factor in (0.5, 1.5):
                with self.subTest(file=path.name, scale=factor):
                    scaled = image.resize((round(image.width * factor), round(image.height * factor)), RESAMPLE)
                    result = detect_crop(scaled)
                    self.assertEqual(result.status, "candidate")
                    for i, value in enumerate(result.box):
                        value /= scaled.width if i % 2 == 0 else scaled.height
                        self.assertAlmostEqual(value, normalized[i], delta=0.01)

    def test_pythonista_bridge_with_mock_not_device(self):
        path = next(self.sample_dir.glob("*.jpg"))
        outputs = []
        fake = types.SimpleNamespace(set_output_image=outputs.append)
        with patch.dict(sys.modules, {"shortcuts": fake}):
            with patch.object(sys, "argv", ["crop_screenshot.py", str(path)]):
                run_pythonista()
        self.assertEqual(len(outputs), 1)
        self.assertIsInstance(outputs[0], Image.Image)

    def test_pythonista_bridge_stops_on_ambiguous_input(self):
        path = next(self.sample_dir.glob("*.jpg"))
        outputs = []
        fake = types.SimpleNamespace(set_output_image=outputs.append)
        with patch.dict(sys.modules, {"shortcuts": fake}):
            with patch.object(sys, "argv", ["crop_screenshot.py", str(path)]):
                with patch("crop_screenshot.open_image", return_value=Image.new("RGB", (100, 200), "white")):
                    with self.assertRaises(RuntimeError):
                        run_pythonista()
        self.assertEqual(outputs, [])


if __name__ == "__main__":
    unittest.main()
