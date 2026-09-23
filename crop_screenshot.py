"""Offline screenshot crop prototype. Input files and Photos are never modified."""

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import html
import json
from pathlib import Path
from statistics import median
import sys
import time

from PIL import Image, ImageDraw, ImageOps

RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR


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
    coverage = sum(count for key, count in counts.items()
                   if _distance(tuple(channel * 16 + 7 for channel in key), color) <= 24)
    coverage /= depth * breadth
    neutral = max(color) <= 55 or min(color) >= 215
    if not neutral or coverage < 0.5:
        return None
    return color


def _profile(image, vertical):
    width, height = image.size
    pixels = image.load()
    backgrounds = [_background(pixels, width, height, vertical, end)
                   for end in (False, True)]
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
    smooth = [median(values[max(0, i - 1):min(length, i + 2)])
              for i in range(length)]
    flags = [value >= 0.28 for value in smooth]
    gap_limit = max(1, round(length * 0.008))
    for start, end in _runs([not flag for flag in flags]):
        if start > 0 and end < length and end - start <= gap_limit:
            flags[start:end] = [True] * (end - start)
    bands = [(start, end) for start, end in _runs(flags)
             if end - start >= length * 0.16]
    if not bands:
        return None, "no_dominant_content_band"
    bands.sort(key=lambda band: band[1] - band[0], reverse=True)
    if len(bands) > 1 and bands[1][1] - bands[1][0] >= (bands[0][1] - bands[0][0]) * 0.45:
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
    """Return a candidate rectangle, not a calibrated confidence probability."""
    width, height = image.size
    original = (0, 0, width, height)
    preview = image.copy()
    preview.thumbnail((384, 768), RESAMPLE)
    preview = preview.convert("RGB")
    box = [0, 0, preview.width, preview.height]
    detected = []
    reasons = []
    for vertical in (True, False):
        # Horizontal analysis uses only the candidate's content, not toolbar bars.
        area = preview if vertical else preview.crop(tuple(box))
        values = _profile(area, vertical)
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
    # Keep one analysis pixel of safety padding instead of eating image edges.
    left = max(0, int((box[0] - 1) * width / preview.width))
    top = max(0, int((box[1] - 1) * height / preview.height))
    right = min(width, int((box[2] + 1) * width / preview.width + 0.999))
    bottom = min(height, int((box[3] + 1) * height / preview.height + 0.999))
    if (right - left) * (bottom - top) < width * height * 0.2:
        return Detection(original, "review", "candidate_too_small", preview.size)
    return Detection((left, top, right, bottom), "candidate",
                     "detected_" + "_and_".join(detected), preview.size)


def open_image(path):
    with Image.open(path) as source:
        return ImageOps.exif_transpose(source).convert("RGB")


def run_pythonista():
    """Input Files supplies paths; no direct Photos writes or deletion here."""
    import shortcuts
    paths = [Path(argument) for argument in sys.argv[1:] if Path(argument).is_file()]
    if len(paths) != 1:
        raise ValueError("Pass exactly one screenshot using Input Files.")
    image = open_image(paths[0])
    result = detect_crop(image)
    if result.status != "candidate":
        raise RuntimeError("Cannot safely locate a single image: " + result.reason)
    shortcuts.set_output_image(image.crop(result.box))


def _write_gallery(output, records):
    entries = []
    contact = Image.new("RGB", (len(records) * 460, 570), "#eff2f6")
    draw = ImageDraw.Draw(contact)
    for index, record in enumerate(records):
        prefix = record["prefix"]
        entries.append(
            '<article><h2>' + html.escape(record["source"]) + '</h2><p>'
            + html.escape(str(record["detection"]["box"])) + ' · '
            + html.escape(record["detection"]["status"]) + '</p><div>'
            + '<figure><img src="' + prefix + '.marked.png"><figcaption>检测边界</figcaption></figure>'
            + '<figure><img src="' + prefix + '.cropped.png"><figcaption>裁剪候选</figcaption></figure>'
            + '</div></article>')
        for column, suffix in enumerate(("marked", "cropped")):
            with Image.open(output / (prefix + "." + suffix + ".png")) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((215, 490), RESAMPLE)
                x = index * 460 + column * 225 + 10
                contact.paste(thumb, (x, 45))
                draw.text((x, 12), str(index + 1) + " " + suffix, fill="#203047")
        draw.text((index * 460 + 10, 540), str(record["output_size"]), fill="#203047")
    contact.save(output / "comparison.jpg", quality=92)
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>截图裁剪实验</title><style>
body{font-family:system-ui;margin:32px;background:#eff2f6;color:#203047}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:24px}
article{background:white;padding:20px;border-radius:16px}h2{font-size:16px;word-break:break-all}
article div{display:flex;gap:12px}figure{margin:0;width:50%}img{width:100%;height:auto}
figcaption{padding:8px 0;color:#567}p{line-height:1.6}</style>
<h1>截图裁剪实验</h1><p>绿色为检测框；原始样本未改动。结果仅为候选，需要人工核验。
图内水印、字幕、遮挡不会被修复。这里没有上传任何截图。</p><main>'''
    (output / "index.html").write_text(document + "".join(entries) + "</main></html>", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Image file or sample directory")
    parser.add_argument("--output", type=Path, required=True, help="New, empty output directory")
    args = parser.parse_args()
    files = [args.input] if args.input.is_file() else sorted(
        path for path in args.input.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    if not files:
        parser.error("No input images found.")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output directory must be empty; existing results are not overwritten.")
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    for index, path in enumerate(files, start=1):
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        start = time.perf_counter()
        image = open_image(path)
        result = detect_crop(image)
        cropped = image.crop(result.box)
        elapsed = time.perf_counter() - start
        prefix = "{:02d}_{}".format(index, path.stem)
        cropped.save(args.output / (prefix + ".cropped.png"))
        marked = image.copy()
        ImageDraw.Draw(marked).rectangle(result.box, outline="#26d07c", width=max(2, image.width // 150))
        marked.save(args.output / (prefix + ".marked.png"))
        unchanged = before == hashlib.sha256(path.read_bytes()).hexdigest()
        record = {"source": path.name, "prefix": prefix, "source_size": image.size,
                  "output_size": cropped.size, "detection": asdict(result),
                  "processing_seconds": round(elapsed, 4), "source_unchanged": unchanged}
        records.append(record)
        print(json.dumps(record, ensure_ascii=False))
    (args.output / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_gallery(args.output, records)


if __name__ == "__main__":
    try:
        import shortcuts
    except ImportError:
        main()
    else:
        if shortcuts.is_running_shortcut():
            run_pythonista()
        else:
            raise RuntimeError("Run via Shortcuts with 'Run in Pythonista' switched off.")
