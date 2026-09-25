#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow",
#   "pillow-heif",
# ]
# ///
"""
Evolution Video Generator
=========================
Creates an "Evolution of <subject>" slideshow video from daily photos.

Usage:
    uv run evolution.py --photos-dir ./photos/Emma --start-date 2024-01-15
    uv run evolution.py --config evolution.toml

Requirements:
    - uv (https://docs.astral.sh/uv/)
    - ffmpeg installed and on PATH  (brew install ffmpeg-full)

Folder structure expected:
    photos/
        Emma/
            2024-01-15.jpg   ← start date = day 0
            2024-01-16.jpg
            ...

Configuration (edit the CONFIG section below or use CLI flags):
    --seconds-per-photo   Duration each photo is shown (default: 2)
    --output-dir          Where to write output videos (default: ./output)
    --max-days            Cap at N days (default: inferred from photo count)
    --crf                 H.265 quality, lower = better (default: 23)
    --resolution          Output resolution WxH (default: 1920x1080)
"""

import argparse
import concurrent.futures
import datetime
import functools
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

# ─── CONFIG DEFAULTS ──────────────────────────────────────────────────────────
SECONDS_PER_PHOTO = 2
OUTPUT_DIR = "./output"
CRF = 23                # H.265 quality (18=high quality, 28=smaller file)
RESOLUTION = "1920x1080"
FPS = 30                # every clip must match: the final concat uses -c copy
DEFAULT_WORKERS = min(os.cpu_count() or 4, 8)
FONT_SIZE_SINGLE = 64   # age label
FONT_SIZE_TITLE = 96
FONT_SIZE_SUBTITLE = 52
FONT_SIZE_SUMMARY = 48  # summary slide and combined video age labels
FONT_SIZE_NAME = 36     # subject name at the top of a combined video panel
TITLE_DURATION = 3      # seconds for title card (fade in + hold + fade out)
TITLE_FADE = 0.6        # seconds for fade in/out
# ──────────────────────────────────────────────────────────────────────────────


def format_age(current_date: datetime.date, start_date: datetime.date) -> str:
    """Return a human-readable age string for a photo taken on *current_date*.

    Uses calendar-aware month arithmetic so that exact month anniversaries
    (e.g. the 6-month mark) display as "6 months" rather than drifting
    by several days due to fixed 30-day month approximations.

    Within each month, remaining days are broken down into weeks and days.

    Examples (start_date = 2024-05-19):
        2024-05-19  → "Day 0"
        2024-05-20  → "1 day"
        2024-05-26  → "1 week"
        2024-05-27  → "1 week 1 day"
        2024-06-02  → "2 weeks"
        2024-06-19  → "1 month"       ← exact 1-month anniversary
        2024-07-19  → "2 months"      ← exact 2-month anniversary
        2024-11-19  → "6 months"      ← exact 6-month anniversary
        2024-11-26  → "6 months 1 week"
    """
    if current_date <= start_date:
        return "Day 0"

    # Whole calendar months elapsed
    months = (
        (current_date.year - start_date.year) * 12
        + (current_date.month - start_date.month)
    )
    day_diff = current_date.day - start_date.day

    if day_diff < 0:
        # Borrow from the previous month using its actual day count
        months -= 1
        prev_month_last = current_date.replace(day=1) - datetime.timedelta(days=1)
        day_diff += prev_month_last.day

    weeks = day_diff // 7
    days = day_diff % 7

    parts = []
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")

    return " ".join(parts) if parts else "Day 0"


def subtitle_from_max_days(max_days: int) -> str:
    """Derive a human-readable title card subtitle from a max_days value.

    Examples:
        30  → "First month"
        180 → "First 6 months"
        183 → "First 183 days"  (183 % 30 != 0; use 180 for clean "6 months")
        365 → "First year"
        730 → "First 2 years"
        400 → "First 400 days"
    """
    if max_days % 365 == 0:
        years = max_days // 365
        return "First year" if years == 1 else f"First {years} years"
    if max_days % 30 == 0:
        months = max_days // 30
        return "First month" if months == 1 else f"First {months} months"
    return f"First {max_days} days"


def ensure_jpeg(photo: Path, tmpdir: Path) -> Path:
    """Return a JPEG-compatible path for ffmpeg.

    For HEIC/HEIF files, converts to a temporary JPEG using pillow-heif.
    All other formats are returned as-is.
    """
    if photo.suffix.lower() not in {".heic", ".heif"}:
        return photo
    import pillow_heif
    pillow_heif.register_heif_opener()
    from PIL import Image
    jpeg_path = tmpdir / (photo.stem + "_heic.jpg")
    if not jpeg_path.exists():
        with Image.open(photo) as img:
            img.convert("RGB").save(jpeg_path, format="JPEG", quality=95)
    return jpeg_path


def find_photos(photos_dir: Path) -> list[tuple[datetime.date, Path]]:
    """Return sorted list of (date, path) for all jpg/png/heic in a directory."""
    extensions = {".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG",
                  ".heic", ".heif", ".HEIC", ".HEIF"}
    photos = []
    for f in photos_dir.iterdir():
        if f.suffix in extensions:
            try:
                date = datetime.date.fromisoformat(f.stem)
                photos.append((date, f))
            except ValueError:
                print(f"  ⚠️  Skipping {f.name} — filename isn't a valid date (expected YYYY-MM-DD)")
    return sorted(photos, key=lambda x: x[0])


def start_date_from_photos(photos: list[tuple[datetime.date, Path]]) -> datetime.date:
    """Infer the start date as the earliest photo date."""
    return photos[0][0]


def ffmpeg_escape(text: str) -> str:
    """Escape text for a drawtext value written as text='<result>'.

    ffmpeg unescapes the value three times, so escape in reverse order:
      1. drawtext text expansion: backslash and % are special.
      2. filter option parser: backslash, quote and : are special.
      3. filtergraph parser: inside '...' nothing can be escaped, so each
         quote closes the string, adds an escaped quote, and reopens it.
    In each step, backslash is escaped first so it doesn't corrupt the
    sequences added after it.
    """
    text = text.replace("\\", "\\\\").replace("%", "\\%")
    text = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return text.replace("'", "'\\''")


def print_progress(completed: int, total: int, bar_width: int = 30) -> None:
    """Print a progress bar, overwriting the current line on a TTY.

    TTY:         \\r  [=============>        ]  68%  clip 125/183
    Non-TTY:     prints a plain line every 30 clips and at completion.
    """
    pct = completed / total if total else 1.0
    filled = int(bar_width * pct)
    arrow = ">" if filled < bar_width else ""
    bar = "=" * filled + arrow + " " * (bar_width - filled - len(arrow))
    if sys.stdout.isatty():
        print(f"\r  [{bar}] {pct:>4.0%}  clip {completed}/{total}", end="", flush=True)
    elif completed % 30 == 0 or completed == total:
        print(f"  ✅ Rendered {completed}/{total} clips")


def check_ffmpeg():
    """Ensure ffmpeg is available."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ ffmpeg not found. Install it with: brew install ffmpeg-full")
        sys.exit(1)


def make_title_card(
    subject_name: str,
    output_path: Path,
    resolution: str = RESOLUTION,
    duration: int = TITLE_DURATION,
    fade: float = TITLE_FADE,
    subtitle: str | None = None,
    crf: int = CRF,
) -> Path:
    """Generate a title card video with fade in/out on black background."""
    w, h = resolution.split("x")
    subtitle_text = subtitle or ""
    title_text = f"Evolution of {subject_name}"

    # Two drawtext filters: title + subtitle
    title_filter = (
        f"color=black:s={resolution}:d={duration}:r={FPS}[base];"
        f"[base]drawtext="
        f"text='{ffmpeg_escape(title_text)}':"
        f"fontsize={FONT_SIZE_TITLE}:"
        f"fontcolor=white:"
        f"x=(w-text_w)/2:y=(h-text_h)/2-60:"
        f"alpha='if(lt(t,{fade}),t/{fade},if(lt(t,{duration - fade}),1,({duration}-t)/{fade}))',"
        f"drawtext="
        f"text='{ffmpeg_escape(subtitle_text)}':"
        f"fontsize={FONT_SIZE_SUBTITLE}:"
        f"fontcolor=0xCCCCCC:"
        f"x=(w-text_w)/2:y=(h-text_h)/2+60:"
        f"alpha='if(lt(t,{fade}),t/{fade},if(lt(t,{duration - fade}),1,({duration}-t)/{fade}))'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-filter_complex", title_filter,
        "-c:v", "libx265",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-tag:v", "hvc1",
        "-t", str(duration),
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


class GridCell(NamedTuple):
    """One panel of a grid clip. A cell with no photo renders as black."""
    photo: Path | None
    top_label: str | None = None
    bottom_label: str | None = None


def _encode_args(crf: int, seconds: int) -> list[str]:
    """Output options shared by every clip so the final concat can use -c copy."""
    return [
        "-c:v", "libx265",
        "-crf", str(crf),
        "-t", str(seconds),
        "-pix_fmt", "yuv420p",
        "-tag:v", "hvc1",
        "-r", str(FPS),
    ]


def make_grid_clip(
    cells: list[list[GridCell]],
    output_path: Path,
    resolution: str = RESOLUTION,
    seconds: int = SECONDS_PER_PHOTO,
    crf: int = CRF,
    top_font_size: int = FONT_SIZE_NAME,
    bottom_font_size: int = FONT_SIZE_SUMMARY,
) -> Path:
    """Render a clip of photos in a rows x cols grid, each with optional labels.

    Each photo is pillarboxed (black bars, no crop) into its cell. The top
    label sits at the top of its cell and the bottom label at the bottom.
    """
    w, h = (int(v) for v in resolution.split("x"))
    rows, cols = len(cells), len(cells[0])
    cell_w, cell_h = w // cols, h // rows

    inputs: list[str] = []
    parts: list[str] = []
    drawtexts: list[str] = []
    label_style = "fontcolor=white:bordercolor=black:borderw=3"
    for r, row in enumerate(cells):
        for c, cell in enumerate(row):
            i = r * cols + c
            if cell.photo is None:
                inputs += ["-f", "lavfi", "-i", f"color=black:s={cell_w}x{cell_h}:r={FPS}"]
            else:
                inputs += ["-loop", "1", "-i", str(cell.photo)]
            parts.append(
                f"[{i}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
                f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2:black[c{i}]"
            )
            x = f"{c * cell_w}+({cell_w}-text_w)/2"
            if cell.top_label:
                drawtexts.append(
                    f"drawtext=text='{ffmpeg_escape(cell.top_label)}':fontsize={top_font_size}:"
                    f"{label_style}:x={x}:y={r * cell_h + 20}"
                )
            if cell.bottom_label:
                drawtexts.append(
                    f"drawtext=text='{ffmpeg_escape(cell.bottom_label)}':fontsize={bottom_font_size}:"
                    f"{label_style}:x={x}:y={(r + 1) * cell_h}-text_h-40"
                )
        row_inputs = "".join(f"[c{r * cols + c}]" for c in range(cols))
        parts.append(f"{row_inputs}hstack=inputs={cols}[r{r}]" if cols > 1 else f"{row_inputs}null[r{r}]")
    row_outputs = "".join(f"[r{r}]" for r in range(rows))
    stack = f"{row_outputs}vstack=inputs={rows}" if rows > 1 else f"{row_outputs}null"
    parts.append(",".join([stack, *drawtexts]))

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(parts),
           *_encode_args(crf, seconds), str(output_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


def make_summary_slide(
    first_jpeg: Path,
    last_jpeg: Path,
    first_age_label: str,
    last_age_label: str,
    output_path: Path,
    resolution: str = RESOLUTION,
    seconds_per_photo: int = SECONDS_PER_PHOTO,
    crf: int = CRF,
) -> Path:
    """Render a side-by-side clip of the first and last photos with age labels."""
    cells = [[GridCell(first_jpeg, bottom_label=first_age_label),
              GridCell(last_jpeg, bottom_label=last_age_label)]]
    return make_grid_clip(cells, output_path, resolution=resolution,
                          seconds=seconds_per_photo, crf=crf)


def load_subject(photos_dir: Path, start_date: datetime.date | None):
    """Return (start_date, photos) for a folder, or None if it has no dated photos."""
    photos = find_photos(photos_dir)
    if not photos:
        print(f"  ❌ No dated photos found in {photos_dir}")
        return None
    if start_date is not None:
        print(f"  📅 Start date (provided): {start_date} ({len(photos)} photos found)")
        return start_date, photos
    start = start_date_from_photos(photos)
    print(f"  📅 Start date (inferred): {start} ({len(photos)} photos found)")
    return start, photos


def photos_by_day(
    photos: list[tuple[datetime.date, Path]],
    start: datetime.date,
    max_days: int,
) -> list[tuple[datetime.date, Path | None]]:
    """Return (date, photo) for each of days 1..max_days counted from *start*.

    A day with no photo reuses the last known photo. Days before the first
    photo get None.
    """
    photo_map = dict(photos)
    days = []
    last_photo = None
    for day_num in range(1, max_days + 1):
        current_date = start + datetime.timedelta(days=day_num - 1)
        last_photo = photo_map.get(current_date, last_photo)
        days.append((current_date, last_photo))
    return days


def render_clips(jobs: list[Callable[[], None]], workers: int) -> None:
    """Run clip render jobs in parallel with a progress bar."""
    total = len(jobs)
    print(f"  🚀 Rendering {total} day clips with {workers} worker(s)...")
    completed = 0
    print_progress(0, total)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for future in concurrent.futures.as_completed(executor.submit(job) for job in jobs):
            future.result()  # re-raise any subprocess exception
            completed += 1
            print_progress(completed, total)
    if sys.stdout.isatty():
        print()  # move past the progress bar line


def concat_clips(clips: list[Path], list_path: Path, output_path: Path) -> None:
    """Join clips in order without re-encoding."""
    with open(list_path, "w") as f:
        for clip in clips:
            f.write(f"file '{clip}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_video(
    photos_dir: Path,
    output_dir: Path,
    seconds_per_photo: int = SECONDS_PER_PHOTO,
    max_days: int | None = None,
    resolution: str = RESOLUTION,
    crf: int = CRF,
    start_date: datetime.date | None = None,
    subtitle: str | None = None,
    workers: int = DEFAULT_WORKERS,
    subject_name: str | None = None,
) -> Path | None:
    """Create an evolution video for one subject.

    *subject_name* defaults to the photos folder name. *max_days* defaults
    to the photo count.
    """
    subject_name = subject_name or photos_dir.name
    print(f"\n🎬 Creating video for {subject_name}...")

    loaded = load_subject(photos_dir, start_date)
    if loaded is None:
        return None
    start, photos = loaded
    if max_days is None:
        max_days = len(photos)

    w, h = resolution.split("x")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        title_path = tmpdir / "title.mp4"
        make_title_card(subject_name, title_path, resolution=resolution, crf=crf,
                        subtitle=subtitle or subtitle_from_max_days(max_days))

        # Pre-convert HEIC files sequentially: ensure_jpeg is not thread-safe
        # for the same source file.
        work_items: list[tuple[Path, str, Path]] = []  # (clip_path, age_label, jpeg_path)
        for day_num, (current_date, photo) in enumerate(photos_by_day(photos, start, max_days), start=1):
            if photo is None:
                continue  # No photo yet (shouldn't happen if start date is correct)
            clip_path = tmpdir / f"clip_{day_num:04d}.mp4"
            work_items.append((clip_path, format_age(current_date, start), ensure_jpeg(photo, tmpdir)))

        def render_clip(clip_path: Path, age_label: str, jpeg_path: Path) -> None:
            vf = (
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"drawtext="
                f"text='{ffmpeg_escape(age_label)}':"
                f"fontsize={FONT_SIZE_SINGLE}:"
                f"fontcolor=white:"
                f"bordercolor=black:borderw=3:"
                f"x=(w-text_w)/2:y=h-text_h-40"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(jpeg_path),
                "-vf", vf,
                *_encode_args(crf, seconds_per_photo),
                str(clip_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        render_clips([functools.partial(render_clip, *item) for item in work_items], workers)

        total_clips = len(work_items)
        output_path = output_dir / f"evolution_{subject_name}.mp4"
        print(f"  🔗 Concatenating {total_clips + 1} clips (1 title card + {total_clips} day clips)...")
        concat_clips([title_path, *(clip for clip, _, _ in work_items)],
                     tmpdir / "clips.txt", output_path)

    print(f"  ✨ Done! → {output_path}")
    return output_path


class Subject(NamedTuple):
    name: str
    photos_dir: Path
    start_date: datetime.date | None = None


def make_combined_video(
    left: Subject,
    right: Subject,
    output_dir: Path,
    seconds_per_photo: int = SECONDS_PER_PHOTO,
    max_days: int | None = None,
    resolution: str = RESOLUTION,
    crf: int = CRF,
    subtitle: str | None = None,
    workers: int = DEFAULT_WORKERS,
) -> Path | None:
    """Create a side-by-side evolution video of two subjects, ending in a 2x2 summary.

    Day N of each panel counts from that subject's own start date.
    *max_days* defaults to the larger photo count: the subject with fewer
    photos repeats its last photo until the end.
    """
    print(f"\n🎬 Creating combined video for {left.name} & {right.name}...")
    loaded = []
    for subject in (left, right):
        print(f"  {subject.name}:")
        result = load_subject(subject.photos_dir, subject.start_date)
        if result is None:
            return None
        loaded.append(result)
    (left_start, left_photos), (right_start, right_photos) = loaded

    if max_days is None:
        max_days = max(len(left_photos), len(right_photos))
        print(f"  📊 Photo counts: {left.name}={len(left_photos)}, {right.name}={len(right_photos)}"
              f" → {max_days} days")

    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        title_path = tmpdir / "title.mp4"
        make_title_card(f"{left.name} & {right.name}", title_path, resolution=resolution, crf=crf,
                        subtitle=subtitle or subtitle_from_max_days(max_days))

        def panel(subject: Subject, start: datetime.date, day: tuple[datetime.date, Path | None]) -> GridCell:
            current_date, photo = day
            if photo is None:
                return GridCell(None, top_label=subject.name)
            return GridCell(ensure_jpeg(photo, tmpdir), subject.name, format_age(current_date, start))

        # Pre-convert HEIC files sequentially: ensure_jpeg is not thread-safe
        # for the same source file.
        work_items: list[tuple[Path, list[list[GridCell]]]] = []
        days = zip(photos_by_day(left_photos, left_start, max_days),
                   photos_by_day(right_photos, right_start, max_days))
        for day_num, (left_day, right_day) in enumerate(days, start=1):
            if left_day[1] is None and right_day[1] is None:
                continue
            cells = [[panel(left, left_start, left_day), panel(right, right_start, right_day)]]
            work_items.append((tmpdir / f"clip_{day_num:04d}.mp4", cells))

        render_clips(
            [functools.partial(make_grid_clip, cells, clip_path, resolution=resolution,
                               seconds=seconds_per_photo, crf=crf)
             for clip_path, cells in work_items],
            workers,
        )

        # Summary: subjects as columns, first photo on the top row, last photo on the bottom.
        first_row, last_row = [], []
        end = datetime.timedelta(days=max_days)
        for subject, (start, photos) in ((left, loaded[0]), (right, loaded[1])):
            shown = [(d, p) for d, p in photos if start <= d < start + end]
            if not shown:
                first_row.append(GridCell(None, top_label=subject.name))
                last_row.append(GridCell(None, top_label=subject.name))
                continue
            for row, (photo_date, photo) in ((first_row, shown[0]), (last_row, shown[-1])):
                row.append(GridCell(ensure_jpeg(photo, tmpdir), subject.name,
                                    format_age(photo_date, start)))
        summary_path = make_grid_clip([first_row, last_row], tmpdir / "summary.mp4",
                                      resolution=resolution, seconds=seconds_per_photo, crf=crf)

        total_clips = len(work_items)
        output_path = output_dir / f"evolution_{left.name}_{right.name}_combined.mp4"
        print(f"  🔗 Concatenating {total_clips + 2} clips "
              f"(1 title card + {total_clips} day clips + 1 summary)...")
        concat_clips([title_path, *(clip for clip, _ in work_items), summary_path],
                     tmpdir / "clips.txt", output_path)

    print(f"  ✨ Done! → {output_path}")
    return output_path


# Config keys. Each setting maps to the make_video() keyword and CLI flag of the same name.
# The subject count picks the mode: 1 subject makes a single video, 2 subjects
# make a combined video only. So every setting except start_date is top-level.
TOP_LEVEL_KEYS = {"output_dir", "seconds_per_photo", "crf", "resolution", "workers", "max_days", "subtitle"}
SUBJECT_KEYS = {"name", "photos_dir", "start_date"}
SETTING_KEYS = TOP_LEVEL_KEYS | {"start_date"}
BUILTIN_SETTINGS = {
    "output_dir": Path(OUTPUT_DIR),
    "seconds_per_photo": SECONDS_PER_PHOTO,
    "max_days": None,
    "crf": CRF,
    "resolution": RESOLUTION,
    "subtitle": None,
    "workers": DEFAULT_WORKERS,
}


class ConfigError(Exception):
    pass


def _normalize_setting(key: str, value, base_dir: Path):
    """Convert a raw TOML value to the type make_video() expects."""
    if key in {"output_dir", "photos_dir"}:
        return (base_dir / value).resolve()
    if key == "start_date" and isinstance(value, str):
        return datetime.date.fromisoformat(value)
    return value


def load_config(config_path: Path) -> tuple[dict, list[dict]]:
    """Read a TOML config file and return (settings, subjects).

    Relative paths resolve against the config file's folder, so the config
    works from any current directory. Unknown keys are an error, to catch typos.
    """
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Cannot read {config_path}: {e}") from e

    base_dir = config_path.resolve().parent
    subjects = raw.pop("subjects", None)
    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"Unknown top-level keys: {', '.join(sorted(unknown))}")
    if not isinstance(subjects, list) or len(subjects) not in (1, 2):
        count = len(subjects) if isinstance(subjects, list) else 0
        raise ConfigError(f"Config needs 1 [[subjects]] table (single video) or 2 (combined video), "
                          f"found {count}")

    settings = {k: _normalize_setting(k, v, base_dir) for k, v in raw.items()}
    normalized = []
    for i, subject in enumerate(subjects, start=1):
        top_only = set(subject) & TOP_LEVEL_KEYS
        if top_only:
            raise ConfigError(f"Subject #{i}: only allowed at the top level: {', '.join(sorted(top_only))}")
        unknown = set(subject) - SUBJECT_KEYS
        if unknown:
            raise ConfigError(f"Subject #{i}: unknown keys: {', '.join(sorted(unknown))}")
        if "photos_dir" not in subject:
            raise ConfigError(f"Subject #{i}: missing required key 'photos_dir'")
        normalized.append({k: _normalize_setting(k, v, base_dir) for k, v in subject.items()})
    return settings, normalized


def main():
    parser = argparse.ArgumentParser(
        description="Generate an 'Evolution of <subject>' slideshow video from daily photos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect start date from earliest photo:
  uv run evolution.py --photos-dir ./photos/Emma

  # Explicit start date:
  uv run evolution.py --photos-dir ./photos/Emma --start-date 2024-01-15

  # Custom duration and quality:
  uv run evolution.py --photos-dir ./photos/Emma --seconds-per-photo 3 --crf 18

  # TOML config file: 1 subject makes a single video,
  # 2 subjects make a combined side-by-side video:
  uv run evolution.py --config evolution.toml

  # CLI flags override config values:
  uv run evolution.py --config evolution.toml --crf 18
        """,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--photos-dir", type=Path,
                        help="Folder containing dated photos (YYYY-MM-DD.jpg) for a single subject.")
    source.add_argument("--config", type=Path,
                        help="TOML config file with 1 [[subjects]] table (single video) or 2 "
                             "(combined side-by-side video: first = left, second = right). "
                             "Relative paths resolve against the config file's folder.")
    # Setting flags default to None so an explicit flag can override config values.
    parser.add_argument("--output-dir", type=Path, default=None,
                        help=f"Where to save the output video (default: {OUTPUT_DIR})")
    parser.add_argument("--seconds-per-photo", type=int, default=None,
                        help=f"Seconds each photo is displayed (default: {SECONDS_PER_PHOTO})")
    parser.add_argument("--max-days", type=int, default=None,
                        help="Maximum days to include (default: inferred from photo count; "
                             "the larger count for a combined video). "
                             "Use to trim a long archive or cap at a milestone.")
    parser.add_argument("--crf", type=int, default=None,
                        help=f"H.265 CRF quality value — lower = better quality/larger file (default: {CRF})")
    parser.add_argument("--resolution", type=str, default=None,
                        help=f"Output resolution WxH (default: {RESOLUTION})")
    parser.add_argument("--subtitle", type=str, default=None,
                        help="Override the title card subtitle (default: derived from --max-days, "
                             "e.g. 'First 6 months')")
    parser.add_argument("--start-date", type=datetime.date.fromisoformat, default=None,
                        help="Override the inferred start date for every subject (format: YYYY-MM-DD). "
                             "Defaults to the date of each subject's earliest photo.")
    parser.add_argument("--workers", type=int, default=None,
                        help=f"Number of parallel ffmpeg workers for clip rendering "
                             f"(default: {DEFAULT_WORKERS}). Use 1 to render sequentially.")

    args = parser.parse_args()

    if args.config:
        try:
            config_settings, subjects = load_config(args.config)
        except ConfigError as e:
            print(f"❌ {e}")
            sys.exit(1)
    else:
        config_settings, subjects = {}, [{"photos_dir": args.photos_dir.resolve()}]

    cli_settings = {k: getattr(args, k) for k in SETTING_KEYS if getattr(args, k) is not None}
    cli_start = cli_settings.pop("start_date", None)
    settings = {**BUILTIN_SETTINGS, **config_settings, **cli_settings}
    settings["output_dir"] = settings["output_dir"].resolve()
    subjects = [
        Subject(s.get("name") or s["photos_dir"].name, s["photos_dir"], cli_start or s.get("start_date"))
        for s in subjects
    ]

    check_ffmpeg()

    if len(subjects) == 1:
        (subject,) = subjects
        output_dir = settings.pop("output_dir")
        result = make_video(subject.photos_dir, output_dir, start_date=subject.start_date,
                            subject_name=subject.name, **settings)
    else:
        result = make_combined_video(*subjects, **settings)

    if result is None:
        print("\n❌ Video failed.")
        sys.exit(1)
    print(f"\n🎉 All done! Video saved to: {result}")


if __name__ == "__main__":
    main()
