#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow",
#   "pillow-heif",
# ]
# ///
"""
Photo Preprocessor
==================
Copies photos from an input directory to an output directory, renaming each
file to YYYY-MM-DD.<ext> so they are ready for baby_evolution.py.

Date is resolved in priority order:
  1. Date parsed from filename — date component used directly, time ignored (camera apps embed local time)
  2. EXIF (tags: DateTimeOriginal, DateTimeDigitized, DateTime) — fallback for files with no parseable filename date (e.g. IMG_NNNN.HEIC)
  3. Filesystem modification time (mtime) — UTC epoch, converted via --timezone (default: UTC)

Files that share a resolved date are skipped — neither is copied.
Files that cannot be dated are skipped with a warning.
Originals are never modified.

Usage:
    uv run preprocess.py --input-dir ./img/Scottie --output-dir ./photos/Scottie
    uv run preprocess.py --input-dir ./img/Scottie --output-dir ./photos/Scottie --dry-run
    uv run preprocess.py --input-dir ./img/Scottie --output-dir ./photos/Scottie --timezone America/New_York
"""

import argparse
import datetime
import re
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png",
    ".heic", ".heif",
    ".JPG", ".JPEG", ".PNG",
    ".HEIC", ".HEIF",
}

# EXIF tag IDs, checked in priority order
_EXIF_DATE_TAGS = (
    36867,  # DateTimeOriginal — when the shutter fired
    36868,  # DateTimeDigitized — when digitised (same as above for most cameras)
    306,    # DateTime — last modification; last resort within EXIF
)

# Matches YYYYMMDD plus an optional _HHMMSS time component in filenames from
# common camera apps. Extra digits after the seconds (e.g. Pixel nanoseconds)
# are consumed and discarded. The (?![0-9]) end assertion prevents matching
# the first 8 digits of a run of digits with no separator.
#
#   20240520_180225.jpg           → date + time
#   IMG_20240730_132546.jpg       → date + time
#   PXL_20240521_183657139.jpg    → date + time (139 = nanoseconds, discarded)
#   PXL_20241119_231141459~2.jpg  → date + time
#   2024-01-15.jpg                → date only
_FILENAME_DATE_RE = re.compile(
    r"(?:^|[^0-9])"
    r"(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"
    r"(?:[_\-](\d{2})(\d{2})(\d{2})\d*)?"  # optional _HHMMSS[nanoseconds]
    r"(?![0-9])"
)


def _exif_date(path: Path) -> datetime.date | None:
    """Extract the earliest reliable date from image EXIF, if available."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()  # public API; works for both JPEG and HEIC
            if not exif:
                return None
            for tag_id in _EXIF_DATE_TAGS:
                raw = exif.get(tag_id)
                if raw:
                    return datetime.datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").date()
    except Exception:
        pass
    return None


def _filename_date(path: Path) -> datetime.date | None:
    """Parse a date from the filename stem.

    Camera apps embed timestamps in local device time (not UTC), so the date
    component is used directly without any timezone conversion. The time
    component, if present, is captured by the regex but intentionally ignored.
    """
    m = _FILENAME_DATE_RE.search(path.stem)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _mtime_date(path: Path, tz: ZoneInfo) -> datetime.date:
    """Return the file's filesystem modification date in *tz*.

    stat().st_mtime is a UTC epoch; converting with the target timezone avoids
    the implicit system-local-timezone conversion of date.fromtimestamp().
    """
    return datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=tz).date()


def resolve_date(path: Path, tz: ZoneInfo) -> tuple[datetime.date, str] | None:
    """Return (date, source_label) using the priority chain, or None if unresolvable.

    Priority: filename → EXIF → mtime.

    Filename is checked first because camera apps embed the local capture date
    directly, while EXIF DateTime (tag 306) on Pixel/Google Photos can be
    overwritten with a UTC-based or export timestamp that is off by one day.
    EXIF covers files with no parseable filename (e.g. IMG_NNNN.HEIC).
    mtime is the last resort for files with neither.
    """
    d = _filename_date(path)
    if d:
        return d, "filename"

    d = _exif_date(path)
    if d:
        return d, "EXIF"

    try:
        return _mtime_date(path, tz), "mtime"
    except OSError:
        return None


def _parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        raise argparse.ArgumentTypeError(
            f"Unknown timezone {name!r}. Use an IANA name like 'America/New_York' or 'UTC'."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename/copy photos to YYYY-MM-DD format for baby_evolution.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run preprocess.py --input-dir ./img/Scottie --output-dir ./photos/Scottie
  uv run preprocess.py --input-dir ./img/Scottie --output-dir ./photos/Scottie --dry-run
  uv run preprocess.py --input-dir ./img/Scottie --output-dir ./photos/Scottie --timezone America/New_York
        """,
    )
    parser.add_argument("--input-dir", type=Path, required=True,
                        help="Directory containing source photos")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Directory to write renamed copies into")
    parser.add_argument("--subject", type=str, default=None,
                        help="Label used in log output (defaults to input dir name)")
    parser.add_argument("--timezone", type=_parse_timezone, default=ZoneInfo("UTC"),
                        metavar="TZ",
                        help="IANA timezone for converting mtime timestamps only "
                             "(default: UTC). EXIF and filename dates are already in local "
                             "time and are not converted. "
                             "Examples: America/New_York, America/Chicago, "
                             "America/Los_Angeles, Europe/London")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing any files")
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    subject = args.subject or input_dir.name
    tz = args.timezone

    if not input_dir.is_dir():
        print(f"❌  Input directory not found: {input_dir}")
        sys.exit(1)

    print(f"\n📂  Subject  : {subject}")
    print(f"    Input    : {input_dir}")
    print(f"    Output   : {output_dir}")
    print(f"    Timezone : {tz.key}")
    print(f"    Mode     : {'DRY RUN — no files will be written' if args.dry_run else 'copy'}\n")

    candidates = sorted(
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix in SUPPORTED_EXTENSIONS
    )

    if not candidates:
        print("⚠️  No supported photo files found.")
        sys.exit(0)

    print(f"🔍  Resolving dates for {len(candidates)} file(s)...")

    # --- Resolve dates -------------------------------------------------------
    resolved: list[tuple[Path, datetime.date, str]] = []  # (path, date, source)
    unresolvable: list[Path] = []

    for path in candidates:
        result = resolve_date(path, tz)
        if result is None:
            unresolvable.append(path)
        else:
            date, source = result
            resolved.append((path, date, source))

    # --- Detect collisions ---------------------------------------------------
    # Group by resolved date; any date with >1 file is a collision
    date_groups: dict[datetime.date, list[tuple[Path, str]]] = {}
    for path, date, source in resolved:
        date_groups.setdefault(date, []).append((path, source))

    to_copy: list[tuple[Path, datetime.date, str]] = []
    collisions: dict[datetime.date, list[Path]] = {}

    for date, group in sorted(date_groups.items()):
        if len(group) == 1:
            path, source = group[0]
            to_copy.append((path, date, source))
        else:
            collisions[date] = [p for p, _ in group]

    # --- Copy ----------------------------------------------------------------
    if not args.dry_run and to_copy:
        output_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped_exists = 0

    for src, date, source in to_copy:
        dest = output_dir / f"{date}{src.suffix.lower()}"
        label = f"[{source}]"

        if not args.dry_run and dest.exists():
            print(f"  ⚠️  {label:10} {src.name}  →  {dest.name}  (already exists, skipped)")
            skipped_exists += 1
            continue

        if args.dry_run:
            print(f"  COPY  {label:10} {src.name}  →  {dest.name}")
        else:
            shutil.copy2(src, dest)
            print(f"  ✅    {label:10} {src.name}  →  {dest.name}")
        copied += 1

    # --- Summary -------------------------------------------------------------
    print(f"\n{'─' * 64}")
    print(f"  {'Would copy' if args.dry_run else 'Copied'}  : {copied} file(s)")
    if skipped_exists:
        print(f"  Skipped  : {skipped_exists} file(s) — destination already exists")
    print(f"  Conflicts: {len(collisions)} date(s) with multiple source files — none copied")
    print(f"  Unknown  : {len(unresolvable)} file(s) — could not determine date")

    if collisions:
        print("\n⚠️  DATE COLLISIONS — resolve manually and re-run:")
        for date, paths in sorted(collisions.items()):
            print(f"\n  {date}:")
            for p in paths:
                print(f"    {p.name}")

    if unresolvable:
        print("\n⚠️  UNRESOLVABLE FILES — no EXIF, no date in filename, no mtime:")
        for p in unresolvable:
            print(f"    {p.name}")

    print()


if __name__ == "__main__":
    main()
