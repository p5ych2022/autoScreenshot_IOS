"""Crop an iOS screenshot using fixed files in Pythonista's iCloud folder."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median
import sys
import time

from PIL import Image, ImageOps

RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR
BUILD = "2026-09-28.2"
BRIDGE_MODE = "shortcut-file"
INPUT_NAME = "shortcut-input.png"
OUTPUT_NAME = "shortcut-output.png"
TEMP_OUTPUT_NAME = ".shortcut-output.tmp"


@dataclass(frozen=True)
class Detection:
    box: tuple
    status: str
    reason: str
    analysis_size: tuple


def _distance(first, second):
    return max(abs(a - b) for a, b in zip(first, second))


def _background(pixels, width, height, vertical, end):
    length = height if vertical else width
    breadth = width if vertical else height
    depth = max(1, round(length * 0.12))
    positions = range(length - depth, length) if end else range(depth)
    counts = Counter()
    for index in positions:
        for cross in range(breadth):
            color = pixels[cross, index] if vertical else pixels[index, cross]
            counts[tuple(channel // 16 for channel in color)] += 1
    bucket, _ = counts.most_common(1)[0]
    color = tuple(channel * 16 + 7 for channel in bucket)
    coverage = sum(
        count
        for key, count in counts.items()
        if _distance(tuple(channel * 16 + 7 for channel in key), color) <= 24
    )
    coverage /= depth * breadth
    neutral = max(color) <= 55 or min(color) >= 215
    if not neutral or coverage < 0.5:
        return None
    return color


def _profile(image, vertical):
    width, height = image.size
    pixels = image.load()
    backgrounds = [
        _background(pixels, width, height, vertical, end)
        for end in (False, True)
    ]
    if any(color is None for color in backgrounds):
        return None
    if _distance(*backgrounds) > 32:
        return None

    length = height if vertical else width
    breadth = width if vertical else height
    values = []
    for index in range(length):
        foreground = 0
        for cross in range(breadth):
            pixel = pixels[cross, index] if vertical else pixels[index, cross]
            if min(_distance(pixel, color) for color in backgrounds) > 24:
                foreground += 1
        values.append(foreground / breadth)
    return values


def _runs(flags):
    runs = []
    start = None
    for index, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    return runs


def _content_band(values):
    length = len(values)
    smooth = [
        median(values[max(0, index - 1):min(length, index + 2)])
        for index in range(length)
    ]
    flags = [value >= 0.28 for value in smooth]
    gap_limit = max(1, round(length * 0.008))
    for start, end in _runs([not flag for flag in flags]):
        if start > 0 and end < length and end - start <= gap_limit:
            flags[start:end] = [True] * (end - start)

    bands = [
        (start, end)
        for start, end in _runs(flags)
        if end - start >= length * 0.16
    ]
    if not bands:
        return None, "no_dominant_content_band"

    bands.sort(key=lambda band: band[1] - band[0], reverse=True)
    if (
        len(bands) > 1
        and bands[1][1] - bands[1][0]
        >= (bands[0][1] - bands[0][0]) * 0.45
    ):
        return None, "multiple_content_bands"

    start, end = bands[0]
    while start > 0 and values[start - 1] > 0.10:
        start -= 1
    while end < length and values[end] > 0.10:
        end += 1
    if start < length * 0.015 or length - end < length * 0.015:
        return None, "missing_two_sided_margin"
    if median(values[start:end]) < 0.4:
        return None, "weak_content_evidence"
    return (start, end), "uniform_margin_and_dominant_band"


def detect_crop(image):
    """Return a conservative crop candidate for uniform black or white margins."""
    width, height = image.size
    original = (0, 0, width, height)
    scale = min(1.0, 384 / width, 768 / height)
    analysis_size = (
        max(1, round(width * scale)),
        max(1, round(height * scale)),
    )
    preview = image.resize(analysis_size, RESAMPLE)
    if preview.mode != "RGB":
        converted = preview.convert("RGB")
        preview.close()
        preview = converted

    try:
        box = [0, 0, preview.width, preview.height]
        detected = []
        reasons = []
        for vertical in (True, False):
            area = preview if vertical else preview.crop(tuple(box))
            try:
                values = _profile(area, vertical)
            finally:
                if area is not preview:
                    area.close()
            if values is None:
                reasons.append("no_uniform_background")
                continue

            band, reason = _content_band(values)
            reasons.append(reason)
            if band is not None:
                start, end = band
                if vertical:
                    box[1], box[3] = start, end
                else:
                    box[0], box[2] = start, end
                detected.append("vertical" if vertical else "horizontal")

        if not detected:
            return Detection(original, "review", ";".join(reasons), preview.size)

        left = max(0, int((box[0] - 1) * width / preview.width))
        top = max(0, int((box[1] - 1) * height / preview.height))
        right = min(width, int((box[2] + 1) * width / preview.width + 0.999))
        bottom = min(height, int((box[3] + 1) * height / preview.height + 0.999))
        if (right - left) * (bottom - top) < width * height * 0.2:
            return Detection(original, "review", "candidate_too_small", preview.size)
        return Detection(
            (left, top, right, bottom),
            "candidate",
            "detected_" + "_and_".join(detected),
            preview.size,
        )
    finally:
        preview.close()


def open_image(path):
    with Image.open(path) as source:
        source.load()
        if source.getexif().get(274, 1) in range(2, 9):
            image = ImageOps.exif_transpose(source)
            source.close()
        else:
            image = source
        if image.mode != "RGB":
            converted = image.convert("RGB")
            image.close()
            return converted
        return image


def _write_stage(stage, details="", reset=False):
    message = "{} {} {}\n".format(time.strftime("%H:%M:%S"), stage, details)
    log_path = Path(__file__).with_name("pythonista_diagnostic.log")
    try:
        with log_path.open("w" if reset else "a", encoding="utf-8") as log:
            log.write(message)
            log.flush()
    except OSError:
        print(message, end="", flush=True)


def run_direct_shortcut():
    """Receive one Input Files image and return the crop to Shortcuts."""
    import shortcuts

    image = None
    cropped = None
    _write_stage("DIRECT_START", "build={}".format(BUILD), reset=True)
    try:
        paths = [Path(argument) for argument in sys.argv[1:] if Path(argument).is_file()]
        _write_stage("INPUT", "files={}".format(len(paths)))
        if len(paths) != 1:
            raise ValueError("Pass exactly one screenshot using Input Files.")

        _write_stage("DECODE_BEGIN", "source=" + paths[0].name)
        image = open_image(paths[0])
        _write_stage("DECODE_DONE", "size={} mode={}".format(image.size, image.mode))

        _write_stage("DETECT_BEGIN")
        result = detect_crop(image)
        _write_stage(
            "DETECT_DONE",
            "status={} box={} reason={}".format(
                result.status,
                result.box,
                result.reason,
            ),
        )
        if result.status != "candidate":
            raise RuntimeError("Cannot safely locate a single image: " + result.reason)

        _write_stage("CROP_BEGIN")
        cropped = image.crop(result.box)
        image.close()
        image = None
        _write_stage("CROP_DONE", "size={}".format(cropped.size))

        _write_stage("OUTPUT_BEGIN")
        shortcuts.set_output_image(cropped)
        _write_stage("OUTPUT_DONE")
    except Exception as error:
        _write_stage("ERROR", type(error).__name__)
        raise
    finally:
        if image is not None:
            image.close()
        if cropped is not None:
            cropped.close()


def _save_png_atomic(image, output, temporary):
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("xb") as stream:
            image.save(stream, format="PNG")
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def run_file_bridge(base_directory=None):
    """Read the fixed iCloud input and atomically replace the fixed output."""
    base = Path(base_directory) if base_directory else Path(__file__).resolve().parent
    source = base / INPUT_NAME
    output = base / OUTPUT_NAME
    temporary = base / TEMP_OUTPUT_NAME
    image = None
    cropped = None

    _write_stage("BRIDGE_START", "build={}".format(BUILD), reset=True)
    output.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    try:
        if not source.is_file():
            raise FileNotFoundError("Missing bridge input: " + str(source))

        _write_stage("DECODE_BEGIN", "source=" + source.name)
        image = open_image(source)
        _write_stage("DECODE_DONE", "size={} mode={}".format(image.size, image.mode))

        _write_stage("DETECT_BEGIN")
        result = detect_crop(image)
        _write_stage(
            "DETECT_DONE",
            "status={} box={} reason={}".format(
                result.status,
                result.box,
                result.reason,
            ),
        )
        if result.status != "candidate":
            raise RuntimeError("Cannot safely locate a single image: " + result.reason)

        _write_stage("CROP_BEGIN")
        cropped = image.crop(result.box)
        image.close()
        image = None
        _write_stage("CROP_DONE", "size={}".format(cropped.size))

        _write_stage("SAVE_BEGIN", "output=" + output.name)
        _save_png_atomic(cropped, output, temporary)
        _write_stage("SAVE_DONE", "size={}".format(output.stat().st_size))
        return output
    except Exception as error:
        output.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        _write_stage("ERROR", type(error).__name__)
        raise
    finally:
        if image is not None:
            image.close()
        if cropped is not None:
            cropped.close()


def main():
    if BRIDGE_MODE in sys.argv[1:]:
        run_file_bridge()
        return

    try:
        import shortcuts
    except ImportError as error:
        raise RuntimeError(
            "Use Input Files in Shortcuts, or run in Pythonista with "
            "Arguments set to 'shortcut-file'."
        ) from error

    if not shortcuts.is_running_shortcut():
        raise RuntimeError(
            "Direct mode requires Run in Pythonista to be off. "
            "File mode requires Arguments='shortcut-file'."
        )
    run_direct_shortcut()


if __name__ == "__main__":
    main()
