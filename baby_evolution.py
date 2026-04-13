#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow",
#   "pillow-heif",
# ]
# ///
"""
Baby Evolution Video Generator
================================
Creates an "Evolution of <subject>" slideshow video from daily photos.

Usage:
    uv run baby_evolution.py --photos-dir ./photos/Emma --birth-date 2024-01-15

Requirements:
    - uv (https://docs.astral.sh/uv/)
    - ffmpeg installed and on PATH  (brew install ffmpeg-full)

Folder structure expected:
    photos/
        Emma/
            2024-01-15.jpg   ← birth date = day 1
            2024-01-16.jpg
            ...

Configuration (edit the CONFIG section below or use CLI flags):
    --seconds-per-photo   Duration each photo is shown (default: 2)
    --output-dir          Where to write output videos (default: ./output)
    --max-days            Cap at N days of life (default: 183 ≈ 6 months)
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
from pathlib import Path

# ─── CONFIG DEFAULTS ──────────────────────────────────────────────────────────
SECONDS_PER_PHOTO = 2
OUTPUT_DIR = "./output"
MAX_DAYS = 183          # ~6 months
CRF = 23                # H.265 quality (18=high quality, 28=smaller file)
RESOLUTION = "1920x1080"
DEFAULT_WORKERS = min(os.cpu_count() or 4, 8)
FONT_SIZE_SINGLE = 64   # age label
FONT_SIZE_TITLE = 96
FONT_SIZE_SUBTITLE = 52
TITLE_DURATION = 3      # seconds for title card (fade in + hold + fade out)
TITLE_FADE = 0.6        # seconds for fade in/out
# ──────────────────────────────────────────────────────────────────────────────


def format_age(current_date: datetime.date, birth_date: datetime.date) -> str:
    """Return a human-readable age string for a photo taken on *current_date*.

    Uses calendar-aware month arithmetic so that exact month anniversaries
    (e.g. the 6-month birthday) display as "6 months" rather than drifting
    by several days due to fixed 30-day month approximations.

    Within each month, remaining days are broken down into weeks and days.

    Examples (birth_date = 2024-05-19):
        2024-05-19  → "Birth"
        2024-05-20  → "1 day"
        2024-05-26  → "1 week"
        2024-05-27  → "1 week 1 day"
        2024-06-02  → "2 weeks"
        2024-06-19  → "1 month"       ← exact 1-month anniversary
        2024-07-19  → "2 months"      ← exact 2-month anniversary
        2024-11-19  → "6 months"      ← exact 6-month anniversary
        2024-11-26  → "6 months 1 week"
    """
    if current_date <= birth_date:
        return "Birth"

    # Whole calendar months elapsed
    months = (
        (current_date.year - birth_date.year) * 12
        + (current_date.month - birth_date.month)
    )
    day_diff = current_date.day - birth_date.day

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

    return " ".join(parts) if parts else "Birth"


def subtitle_from_max_days(max_days: int) -> str:
    """Derive a human-readable title card subtitle from a max_days value.

    Examples:
        30  → "Birth to 1 month"
        180 → "Birth to 6 months"
        183 → "Birth to 183 days"  (183 % 30 != 0; use 180 for clean "6 months")
        365 → "Birth to 1 year"
        400 → "Birth to 400 days"
    """
    if max_days % 365 == 0:
        years = max_days // 365
        return f"Birth to {years} year{'s' if years != 1 else ''}"
    if max_days % 30 == 0:
        months = max_days // 30
        return f"Birth to {months} month{'s' if months != 1 else ''}"
    return f"Birth to {max_days} days"


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


def birth_date_from_photos(photos: list[tuple[datetime.date, Path]]) -> datetime.date:
    """Infer birth date as the earliest photo date."""
    return photos[0][0]


def ffmpeg_escape(text: str) -> str:
    """Escape text for ffmpeg drawtext filter."""
    # Backslash must be escaped first so it doesn't corrupt the sequences added below.
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def check_ffmpeg():
    """Ensure ffmpeg is available."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ ffmpeg not found. Install it with: brew install ffmpeg-full")
        sys.exit(1)


def make_title_card(
    child_name: str,
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
    title_text = f"Evolution of {child_name}"

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


def make_single_child_video(
    photos_dir: Path,
    output_dir: Path,
    seconds_per_photo: int = SECONDS_PER_PHOTO,
    max_days: int = MAX_DAYS,
    resolution: str = RESOLUTION,
    crf: int = CRF,
    birth_date: datetime.date | None = None,
    subtitle: str | None = None,
    workers: int = DEFAULT_WORKERS,
) -> Path | None:
    """Create a single-child evolution video."""
    child_name = photos_dir.name
    print(f"\n🎬 Creating video for {child_name}...")

    photos = find_photos(photos_dir)
    if not photos:
        print(f"  ❌ No dated photos found in {photos_dir}")
        return None

    if birth_date is not None:
        birth = birth_date
        print(f"  📅 Birth date (provided): {birth} ({len(photos)} photos found)")
    else:
        birth = birth_date_from_photos(photos)
        print(f"  📅 Birth date (inferred): {birth} ({len(photos)} photos found)")

    # Build a lookup: date → path
    photo_map = {date: path for date, path in photos}

    w, h = resolution.split("x")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        clip_list_path = tmpdir / "clips.txt"

        # Title card (sequential — single call, no parallelism benefit)
        title_path = tmpdir / "title.mp4"
        make_title_card(child_name, title_path, resolution=resolution, crf=crf,
                        subtitle=subtitle or subtitle_from_max_days(max_days))

        # Build ordered work list and pre-convert any HEIC files sequentially
        # (ensure_jpeg is not thread-safe for the same source file, so do it here)
        work_items: list[tuple[int, Path, str, Path]] = []  # (day_num, clip_path, age_label, jpeg_path)
        last_photo = None
        for day_num in range(1, max_days + 1):
            current_date = birth + datetime.timedelta(days=day_num - 1)
            photo = photo_map.get(current_date)

            if photo:
                last_photo = photo
            elif last_photo:
                # Use previous day's photo if missing
                photo = last_photo
            else:
                continue  # No photo yet (shouldn't happen if birth date is correct)

            age_label = format_age(current_date, birth)
            clip_path = tmpdir / f"clip_{day_num:04d}.mp4"
            jpeg_path = ensure_jpeg(photo, tmpdir)  # HEIC conversion here (deduped, sequential)
            work_items.append((day_num, clip_path, age_label, jpeg_path))

        total_clips = len(work_items)
        print(f"  🚀 Rendering {total_clips} clips with {workers} worker(s)...")

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
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(render_clip, item): item[0] for item in work_items}
            for future in concurrent.futures.as_completed(futures):
                future.result()  # re-raise any subprocess exception
                completed += 1
                if completed % 30 == 0:
                    print(f"  ✅ Rendered {completed}/{total_clips} clips")

        # Write concat list in day order after all clips are ready
        with open(clip_list_path, "w") as concat_f:
            concat_f.write(f"file '{title_path}'\n")
            for _, clip_path, _, _ in work_items:
                concat_f.write(f"file '{clip_path}'\n")

        output_path = output_dir / f"evolution_{child_name}.mp4"
        print(f"  🔗 Concatenating {total_clips + 1} clips...")
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


def main():
    parser = argparse.ArgumentParser(
        description="Generate an 'Evolution of <subject>' slideshow video from daily photos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-detect birth date from earliest photo:
  uv run baby_evolution.py --photos-dir ./photos/Emma

  # Explicit birth date:
  uv run baby_evolution.py --photos-dir ./photos/Emma --birth-date 2024-01-15

  # Custom duration and quality:
  uv run baby_evolution.py --photos-dir ./photos/Emma --seconds-per-photo 3 --crf 18
        """,
    )
    parser.add_argument("--photos-dir", type=Path, required=True,
                        help="Folder containing dated photos (YYYY-MM-DD.jpg) for a single subject.")
    parser.add_argument("--output-dir", type=Path, default=Path(OUTPUT_DIR),
                        help=f"Where to save the output video (default: {OUTPUT_DIR})")
    parser.add_argument("--seconds-per-photo", type=int, default=SECONDS_PER_PHOTO,
                        help=f"Seconds each photo is displayed (default: {SECONDS_PER_PHOTO})")
    parser.add_argument("--max-days", type=int, default=MAX_DAYS,
                        help=f"Maximum days to include (default: {MAX_DAYS} ≈ 6 months)")
    parser.add_argument("--crf", type=int, default=CRF,
                        help=f"H.265 CRF quality value — lower = better quality/larger file (default: {CRF})")
    parser.add_argument("--resolution", type=str, default=RESOLUTION,
                        help=f"Output resolution WxH (default: {RESOLUTION})")
    parser.add_argument("--subtitle", type=str, default=None,
                        help="Override the title card subtitle (default: derived from --max-days, "
                             "e.g. 'Birth to 6 months')")
    parser.add_argument("--birth-date", type=datetime.date.fromisoformat,
                        help="Override the inferred birth date (format: YYYY-MM-DD). "
                             "Defaults to the date of the earliest photo.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Number of parallel ffmpeg workers for clip rendering "
                             f"(default: {DEFAULT_WORKERS}). Use 1 to render sequentially.")

    args = parser.parse_args()
    check_ffmpeg()

    photos_dir = args.photos_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not find_photos(photos_dir):
        print(f"❌ No dated photos found in {photos_dir}")
        sys.exit(1)

    make_single_child_video(
        photos_dir,
        output_dir,
        seconds_per_photo=args.seconds_per_photo,
        max_days=args.max_days,
        resolution=args.resolution,
        crf=args.crf,
        birth_date=args.birth_date,
        subtitle=args.subtitle,
        workers=args.workers,
    )

    print(f"\n🎉 All done! Video saved to: {output_dir}")


if __name__ == "__main__":
    main()
