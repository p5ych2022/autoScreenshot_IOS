"""Regression checks; synthetic cases do not establish real-device accuracy."""

import hashlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from crop_screenshot import RESAMPLE, detect_crop, open_image, run_pythonista, run_pythonista_local


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


class ImageLoadingTests(unittest.TestCase):
    def test_loaded_pixels_survive_file_context(self):
        buffer = io.BytesIO()
        Image.new("RGB", (20, 30), (12, 34, 56)).save(buffer, format="PNG")
        buffer.seek(0)
        image = open_image(buffer)
        self.assertEqual(image.getpixel((1, 1)), (12, 34, 56))
        self.assertEqual(image.size, (20, 30))
        image.close()

    def test_rgba_and_grayscale_convert_to_rgb(self):
        for mode, color, expected in (("RGBA", (12, 34, 56, 255), (12, 34, 56)),
                                       ("L", 99, (99, 99, 99))):
            with self.subTest(mode=mode):
                buffer = io.BytesIO()
                Image.new(mode, (20, 30), color).save(buffer, format="PNG")
                buffer.seek(0)
                image = open_image(buffer)
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.getpixel((0, 0)), expected)
                image.close()

    def test_exif_orientation_is_applied(self):
        buffer = io.BytesIO()
        source = Image.new("RGB", (20, 30), "red")
        exif = Image.Exif()
        exif[274] = 6
        source.save(buffer, format="PNG", exif=exif)
        buffer.seek(0)
        image = open_image(buffer)
        self.assertEqual(image.size, (30, 20))
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        self.assertNotIn(274, image.getexif())
        image.close()

    def test_detection_does_not_copy_full_resolution_image(self):
        image = Image.new("RGB", (1200, 2400), "black")
        ImageDraw.Draw(image).rectangle((0, 300, 1199, 2099), fill="orange")
        with patch.object(image, "copy", side_effect=AssertionError("Full-size copy")):
            result = detect_crop(image)
        self.assertEqual(result.status, "candidate")
        self.assertEqual(image.size, (1200, 2400))


class ProvidedSampleTests(unittest.TestCase):
    sample_dir = Path(__file__).parent / "sample"

    def setUp(self):
        logger = patch("crop_screenshot._write_stage")
        self.stages = logger.start()
        self.addCleanup(logger.stop)

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

    def test_multiple_inputs_stop_before_decode(self):
        paths = sorted(self.sample_dir.glob("*.jpg"))
        fake = types.SimpleNamespace(set_output_image=lambda image: self.fail("Unexpected output"))
        with patch.dict(sys.modules, {"shortcuts": fake}):
            with patch.object(sys, "argv", ["crop_screenshot.py"] + [str(path) for path in paths]):
                with patch("crop_screenshot.open_image") as decode:
                    with self.assertRaises(ValueError):
                        run_pythonista()
                    decode.assert_not_called()
        self.stages.assert_any_call("ERROR", "ValueError")

    def test_diagnose_returns_text_without_image_output(self):
        path = next(self.sample_dir.glob("*.jpg"))
        fake = types.SimpleNamespace(set_output_image=lambda image: self.fail("Unexpected image output"))
        with patch.dict(sys.modules, {"shortcuts": fake}):
            with patch.object(sys, "argv", ["crop_screenshot.py", "diagnose", str(path)]):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    run_pythonista()
        self.assertIn("Detection finished", output.getvalue())
        self.stages.assert_any_call("TEXT_OUTPUT_DONE")

    def test_original_is_released_before_image_output(self):
        path = next(self.sample_dir.glob("*.jpg"))
        original = open_image(path)
        result = detect_crop(original)
        expected = original.getpixel(result.box[:2])
        outputs = []

        def receive(image):
            with self.assertRaises(ValueError):
                original.getpixel((0, 0))
            self.assertEqual(image.getpixel((0, 0)), expected)
            self.assertEqual(image.width, result.box[2] - result.box[0])
            outputs.append(image)

        fake = types.SimpleNamespace(set_output_image=receive)
        with patch.dict(sys.modules, {"shortcuts": fake}):
            with patch.object(sys, "argv", ["crop_screenshot.py", str(path)]):
                with patch("crop_screenshot.open_image", return_value=original):
                    run_pythonista()
        self.assertEqual(len(outputs), 1)
        self.stages.assert_any_call("OUTPUT_DONE")

    def test_output_exception_leaves_stage_evidence(self):
        path = next(self.sample_dir.glob("*.jpg"))

        def fail(image):
            raise RuntimeError("Simulated native output failure")

        fake = types.SimpleNamespace(set_output_image=fail)
        with patch.dict(sys.modules, {"shortcuts": fake}):
            with patch.object(sys, "argv", ["crop_screenshot.py", str(path)]):
                with self.assertRaises(RuntimeError):
                    run_pythonista()
        stages = [call.args[0] for call in self.stages.call_args_list]
        self.assertEqual(stages[-2:], ["OUTPUT_BEGIN", "ERROR"])
        self.assertNotIn("OUTPUT_DONE", stages)


class LocalPreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "IMG_2844.PNG"
        image = Image.new("RGB", (300, 600), "black")
        ImageDraw.Draw(image).rectangle((0, 90, 299, 479), fill="orange")
        image.save(self.source)
        image.close()
        self.before = self.source.read_bytes()
        self.preview = Mock()
        patches = [
            patch.dict(sys.modules, {"console": types.SimpleNamespace(quicklook=self.preview)}),
            patch("crop_screenshot.__file__", str(self.root / "crop_screenshot.py")),
            patch("crop_screenshot._write_stage"),
            patch.object(sys, "argv", ["crop_screenshot.py"]),
            patch("sys.stdout", new_callable=io.StringIO),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_default_sibling_file_is_saved_and_previewed(self):
        output = run_pythonista_local()
        self.assertEqual(output, self.root / "IMG_2844.cropped.png")
        self.preview.assert_called_once_with(str(output))
        with Image.open(output) as cropped:
            self.assertEqual(cropped.width, 300)
            self.assertLess(cropped.height, 600)
            self.assertEqual(cropped.getpixel((100, 100)), (255, 165, 0))
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_repeat_runs_do_not_overwrite_existing_output(self):
        first = run_pythonista_local()
        original = first.read_bytes()
        second = run_pythonista_local()
        self.assertEqual(second.name, "IMG_2844.cropped-2.png")
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_missing_file_raises_without_preview(self):
        with self.assertRaises(FileNotFoundError):
            run_pythonista_local("missing.png")
        self.preview.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_ambiguous_image_produces_no_file_or_preview(self):
        Image.new("RGB", (300, 600), "white").save(self.source)
        self.assertIsNone(run_pythonista_local())
        self.preview.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_explicit_input_path(self):
        with patch.object(sys, "argv", ["crop_screenshot.py", str(self.source)]):
            output = run_pythonista_local()
        self.assertTrue(output.is_file())

    def test_failed_save_removes_only_new_partial_output(self):
        with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run_pythonista_local()
        self.preview.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.source])
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_preview_error_keeps_saved_image(self):
        self.preview.side_effect = RuntimeError("preview unavailable")
        with self.assertRaises(RuntimeError):
            run_pythonista_local()
        output = self.root / "IMG_2844.cropped.png"
        with Image.open(output) as cropped:
            self.assertEqual(cropped.width, 300)
        self.assertEqual(self.source.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
