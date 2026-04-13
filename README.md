# evolution-video

Creates an "Evolution of \<name\>" slideshow video from a folder of daily photos.

## Requirements

- [uv](https://docs.astral.sh/uv/) — runs the script with no manual dependency installation
- [ffmpeg](https://ffmpeg.org/) — `brew install ffmpeg-full` (the standard `ffmpeg` bottle omits `libfreetype`, which is required for text overlays)

## Workflow

```
raw photos (any filenames)
        │
        ▼
  preprocess.py          ← renames copies to YYYY-MM-DD format
        │
        ▼
dated photos folder
        │
        ▼
 baby_evolution.py       ← generates the video
```

## Folder structure

`baby_evolution.py` expects photos named `YYYY-MM-DD.jpg` (or `.jpeg` / `.png` / `.heic` / `.heif`) in a folder named after the subject:

```
photos/
    Emma/
        2024-01-15.jpg    ← earliest photo = day 1
        2024-01-16.jpg
        2024-01-18.jpg    ← gaps are fine; last known photo is reused
        ...
```

If your photos have camera-generated filenames, use `preprocess.py` to prepare them first (see below).

## Step 1 — Preprocess photos (if needed)

`preprocess.py` copies photos from a raw input directory into a dated output directory, renaming each file to `YYYY-MM-DD.<ext>`. Originals are never modified.

Date is resolved in priority order:
1. Date embedded in the filename (`YYYYMMDD_*`, `IMG_YYYYMMDD_*`, `PXL_YYYYMMDD_*`, etc.) — camera apps write local time directly into the filename
2. EXIF — fallback for files with no parseable filename date (e.g. `IMG_NNNN.HEIC`)
3. Filesystem modification time — last resort, converted via `--timezone`

If multiple photos resolve to the same date, **neither is copied** — the conflict is reported at the end for manual resolution.

```bash
# Preview what would happen (no files written)
uv run preprocess.py --input-dir ./img/Emma --output-dir ./photos/Emma --dry-run

# Run for real
uv run preprocess.py --input-dir ./img/Emma --output-dir ./photos/Emma

# Specify the timezone for mtime fallback conversion
uv run preprocess.py --input-dir ./img/Emma --output-dir ./photos/Emma --timezone America/New_York
```

### Preprocess options

| Flag | Default | Description |
|---|---|---|
| `--input-dir` | *(required)* | Directory containing source photos |
| `--output-dir` | *(required)* | Directory to write renamed copies into |
| `--timezone` | `UTC` | IANA timezone for converting mtime timestamps only (e.g. `America/New_York`, `America/Chicago`, `America/Los_Angeles`). EXIF and filename dates are already in local time and are not converted. |
| `--subject` | *(input dir name)* | Label used in log output |
| `--dry-run` | off | Print what would be copied without writing anything |

## Step 2 — Generate the video

```bash
# Birth date inferred from earliest photo
uv run baby_evolution.py --photos-dir ./photos/Emma

# Explicit birth date
uv run baby_evolution.py --photos-dir ./photos/Emma --birth-date 2024-01-15
```

Output videos are written to `./output/` by default.

## Options

| Flag | Default | Description |
|---|---|---|
| `--photos-dir` | *(required)* | Folder containing dated photos for a single subject |
| `--output-dir` | `./output` | Where to write the output video |
| `--seconds-per-photo` | `2` | How long each photo is shown |
| `--max-days` | `183` | Number of days to cover (~6 months) |
| `--crf` | `23` | H.265 quality — lower = better quality, larger file (18–28 is typical) |
| `--resolution` | `1920x1080` | Output resolution |
| `--subtitle` | *(derived from `--max-days`)* | Title card subtitle, e.g. `"First year"` |
| `--birth-date` | *(inferred from earliest photo)* | Override birth date (`YYYY-MM-DD`) |
