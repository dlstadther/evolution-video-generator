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
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

# ─── CONFIG DEFAULTS ──────────────────────────────────────────────────────────
SECONDS_PER_PHOTO = 2
OUTPUT_DIR = "./output"
CRF = 23                # H.265 quality (18=high quality, 28=smaller file)
RESOLUTION = "1920x1080"
DEFAULT_WORKERS = min(os.cpu_count() or 4, 8)
FONT_SIZE_SINGLE = 64   # age label
FONT_SIZE_TITLE = 96
FONT_SIZE_SUBTITLE = 52
FONT_SIZE_SUMMARY = 48  # summary slide labels
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
        f"color=black:s={resolution}:d={duration}[base];"
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
    """Render a side-by-side clip of the first and last photos with age labels.

    Each photo is pillarboxed (black bars, no crop) into half the frame width.
    """
    w, h = (int(v) for v in resolution.split("x"))
    half_w = w // 2
    label_style = (
        f"fontsize={FONT_SIZE_SUMMARY}:"
        f"fontcolor=white:"
        f"bordercolor=black:borderw=3:"
        f"y=h-text_h-40"
    )
    panel = f"scale={half_w}:{h}:force_original_aspect_ratio=decrease,pad={half_w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    filter_complex = (
        f"[0:v]{panel}[left];"
        f"[1:v]{panel}[right];"
        f"[left][right]hstack=inputs=2,"
        f"drawtext=text='{ffmpeg_escape(first_age_label)}':{label_style}:x=({half_w}-text_w)/2,"
        f"drawtext=text='{ffmpeg_escape(last_age_label)}':{label_style}:x={half_w}+({half_w}-text_w)/2"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(first_jpeg),
        "-loop", "1", "-i", str(last_jpeg),
        "-filter_complex", filter_complex,
        "-c:v", "libx265",
        "-crf", str(crf),
        "-t", str(seconds_per_photo),
        "-pix_fmt", "yuv420p",
        "-tag:v", "hvc1",
        "-r", "30",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


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

    photos = find_photos(photos_dir)
    if not photos:
        print(f"  ❌ No dated photos found in {photos_dir}")
        return None

    if max_days is None:
        max_days = len(photos)

    if start_date is not None:
        start = start_date
        print(f"  📅 Start date (provided): {start} ({len(photos)} photos found)")
    else:
        start = start_date_from_photos(photos)
        print(f"  📅 Start date (inferred): {start} ({len(photos)} photos found)")

    # Build a lookup: date → path
    photo_map = {date: path for date, path in photos}

    w, h = resolution.split("x")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        clip_list_path = tmpdir / "clips.txt"

        # Title card (sequential — single call, no parallelism benefit)
        title_path = tmpdir / "title.mp4"
        make_title_card(subject_name, title_path, resolution=resolution, crf=crf,
                        subtitle=subtitle or subtitle_from_max_days(max_days))

        # Build ordered work list and pre-convert any HEIC files sequentially
        # (ensure_jpeg is not thread-safe for the same source file, so do it here)
        work_items: list[tuple[int, Path, str, Path]] = []  # (day_num, clip_path, age_label, jpeg_path)
        last_photo = None
        for day_num in range(1, max_days + 1):
            current_date = start + datetime.timedelta(days=day_num - 1)
            photo = photo_map.get(current_date)

            if photo:
                last_photo = photo
            elif last_photo:
                # Use previous day's photo if missing
                photo = last_photo
            else:
                continue  # No photo yet (shouldn't happen if start date is correct)

            age_label = format_age(current_date, start)
            clip_path = tmpdir / f"clip_{day_num:04d}.mp4"
            jpeg_path = ensure_jpeg(photo, tmpdir)  # HEIC conversion here (deduped, sequential)
            work_items.append((day_num, clip_path, age_label, jpeg_path))

        total_clips = len(work_items)
        print(f"  🚀 Rendering {total_clips} day clips with {workers} worker(s)...")

        def render_clip(item: tuple[int, Path, str, Path]) -> None:
            _, clip_path, age_label, jpeg_path = item
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
                "-c:v", "libx265",
                "-crf", str(crf),
                "-t", str(seconds_per_photo),
                "-pix_fmt", "yuv420p",
                "-tag:v", "hvc1",
                "-r", "30",
                str(clip_path),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        completed = 0
        print_progress(0, total_clips)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(render_clip, item): item[0] for item in work_items}
            for future in concurrent.futures.as_completed(futures):
                future.result()  # re-raise any subprocess exception
                completed += 1
                print_progress(completed, total_clips)
        if sys.stdout.isatty():
            print()  # move past the progress bar line

        # Write concat list in day order after all clips are ready
        with open(clip_list_path, "w") as concat_f:
            concat_f.write(f"file '{title_path}'\n")
            for _, clip_path, _, _ in work_items:
                concat_f.write(f"file '{clip_path}'\n")

        output_path = output_dir / f"evolution_{subject_name}.mp4"
        print(f"  🔗 Concatenating {total_clips + 1} clips (1 title card + {total_clips} day clips)...")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(clip_list_path),
            "-c", "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"  ✨ Done! → {output_path}")
    return output_path


# Settings allowed at the top level of a config file and inside each [[subjects]] table.
# Each maps to the make_video() keyword and CLI flag of the same name.
SETTING_KEYS = {
    "output_dir", "seconds_per_photo", "max_days", "crf",
    "resolution", "subtitle", "start_date", "workers",
}
SUBJECT_KEYS = {"name", "photos_dir"}
BUILTIN_SETTINGS = {
    "output_dir": Path(OUTPUT_DIR),
    "seconds_per_photo": SECONDS_PER_PHOTO,
    "max_days": None,
    "crf": CRF,
    "resolution": RESOLUTION,
    "subtitle": None,
    "start_date": None,
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


def load_config(config_path: Path) -> list[dict]:
    """Read a TOML config file and return one settings dict per subject.

    Relative paths resolve against the config file's folder, so the config
    works from any current directory. Per-subject values override top-level
    values. Unknown keys are an error, to catch typos.
    """
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Cannot read {config_path}: {e}") from e

    base_dir = config_path.resolve().parent
    subjects = raw.pop("subjects", None)
    unknown = set(raw) - SETTING_KEYS
    if unknown:
        raise ConfigError(f"Unknown top-level keys: {', '.join(sorted(unknown))}")
    if not isinstance(subjects, list) or not subjects:
        raise ConfigError("Config needs at least one [[subjects]] table")

    top = {k: _normalize_setting(k, v, base_dir) for k, v in raw.items()}
    jobs = []
    for i, subject in enumerate(subjects, start=1):
        unknown = set(subject) - SETTING_KEYS - SUBJECT_KEYS
        if unknown:
            raise ConfigError(f"Subject #{i}: unknown keys: {', '.join(sorted(unknown))}")
        if "photos_dir" not in subject:
            raise ConfigError(f"Subject #{i}: missing required key 'photos_dir'")
        job = dict(top)
        job.update({k: _normalize_setting(k, v, base_dir) for k, v in subject.items()})
        jobs.append(job)
    return jobs


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

  # One video per subject listed in a TOML config file:
  uv run evolution.py --config evolution.toml

  # CLI flags override config values for every subject:
  uv run evolution.py --config evolution.toml --crf 18
        """,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--photos-dir", type=Path,
                        help="Folder containing dated photos (YYYY-MM-DD.jpg) for a single subject.")
    source.add_argument("--config", type=Path,
                        help="TOML config file with one [[subjects]] table per subject. "
                             "Relative paths resolve against the config file's folder.")
    # Setting flags default to None so an explicit flag can override config values.
    parser.add_argument("--output-dir", type=Path, default=None,
                        help=f"Where to save the output video (default: {OUTPUT_DIR})")
    parser.add_argument("--seconds-per-photo", type=int, default=None,
                        help=f"Seconds each photo is displayed (default: {SECONDS_PER_PHOTO})")
    parser.add_argument("--max-days", type=int, default=None,
                        help="Maximum days to include (default: inferred from photo count). "
                             "Use to trim a long archive or cap at a milestone.")
    parser.add_argument("--crf", type=int, default=None,
                        help=f"H.265 CRF quality value — lower = better quality/larger file (default: {CRF})")
    parser.add_argument("--resolution", type=str, default=None,
                        help=f"Output resolution WxH (default: {RESOLUTION})")
    parser.add_argument("--subtitle", type=str, default=None,
                        help="Override the title card subtitle (default: derived from --max-days, "
                             "e.g. 'First 6 months')")
    parser.add_argument("--start-date", type=datetime.date.fromisoformat, default=None,
                        help="Override the inferred start date (format: YYYY-MM-DD). "
                             "Defaults to the date of the earliest photo.")
    parser.add_argument("--workers", type=int, default=None,
                        help=f"Number of parallel ffmpeg workers for clip rendering "
                             f"(default: {DEFAULT_WORKERS}). Use 1 to render sequentially.")

    args = parser.parse_args()

    if args.config:
        try:
            jobs = load_config(args.config)
        except ConfigError as e:
            print(f"❌ {e}")
            sys.exit(1)
    else:
        jobs = [{"photos_dir": args.photos_dir.resolve()}]

    cli_settings = {k: getattr(args, k) for k in SETTING_KEYS if getattr(args, k) is not None}

    check_ffmpeg()

    failed = []
    for job in jobs:
        settings = {**BUILTIN_SETTINGS, **job, **cli_settings}
        photos_dir = settings.pop("photos_dir")
        output_dir = settings.pop("output_dir").resolve()
        result = make_video(photos_dir, output_dir, subject_name=settings.pop("name", None), **settings)
        if result is None:
            failed.append(photos_dir)

    if failed:
        print(f"\n❌ {len(failed)} of {len(jobs)} video(s) failed: "
              + ", ".join(str(p) for p in failed))
        sys.exit(1)
    print(f"\n🎉 All done! {len(jobs)} video(s) saved.")


if __name__ == "__main__":
    main()
