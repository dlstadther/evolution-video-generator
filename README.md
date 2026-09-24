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
 evolution.py       ← generates the video
```

## Folder structure

`evolution.py` expects photos named `YYYY-MM-DD.jpg` (or `.jpeg` / `.png` / `.heic` / `.heif`) in a folder named after the subject:

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
# Start date inferred from earliest photo
uv run evolution.py --photos-dir ./photos/Emma

# Explicit start date
uv run evolution.py --photos-dir ./photos/Emma --start-date 2024-01-15
```

Output videos are written to `./output/` by default.

## Configuration file

To make videos for many subjects in one run, put the settings in a TOML file and pass it with `--config`. The script makes one video for each `[[subjects]]` table.

```toml
# evolution.toml
output_dir = "./output"
seconds_per_photo = 2

[[subjects]]
name = "Emma"                  # optional, defaults to the photos_dir folder name
photos_dir = "./photos/Emma"   # required
start_date = 2024-01-15

[[subjects]]
photos_dir = "./photos/Noah"
max_days = 180
```

```bash
uv run evolution.py --config evolution.toml

# A CLI flag overrides the config value for every subject
uv run evolution.py --config evolution.toml --crf 18
```

Rules:

- These keys are top-level only: `output_dir`, `seconds_per_photo`, `crf`, `resolution`, `workers` and `combined`.
- These keys are allowed at the top level and in `[[subjects]]`: `max_days`, `subtitle` and `start_date`. A top-level value is the default for every subject. A value inside `[[subjects]]` overrides it for that subject.
- Each subject also takes `photos_dir` (required) and `name`.
- A CLI flag overrides the config value.
- Relative paths resolve against the folder that contains the config file.
- An unknown key, or a top-level-only key inside `[[subjects]]`, is an error.

## Combined side-by-side video

A combined video shows two subjects side by side, day by day. Use a config file with exactly two `[[subjects]]` tables. The first subject is the left panel, and the second subject is the right panel.

```toml
# pair.toml
combined = "also"   # "also": individual videos + combined video. "only": combined video only.

[[subjects]]
photos_dir = "./photos/Emma"

[[subjects]]
photos_dir = "./photos/Noah"
```

```bash
uv run evolution.py --config pair.toml

# The CLI flags override the config value
uv run evolution.py --config pair.toml --combined
uv run evolution.py --config pair.toml --combined-only
```

The combined video:

- Starts with the title card "Evolution of Emma & Noah".
- Shows each subject's name at the top of its panel and its age at the bottom. Day N of each panel counts from that subject's own start date.
- Covers the larger photo count of the two subjects. The subject with fewer photos repeats its last photo until the end. To set a different length, use a top-level `max_days` or `--max-days`.
- Ends with a 2x2 summary slide. The columns are the subjects. The top row shows the first photos, and the bottom row shows the last photos.
- Uses only top-level and CLI settings. A per-subject `max_days` or `subtitle` applies only to that subject's own video.
- Is written to `evolution_<left>_<right>_combined.mp4`.

## Options

| Flag | Default | Description |
|---|---|---|
| `--photos-dir` | *(required unless `--config`)* | Folder containing dated photos for a single subject |
| `--config` | *(none)* | TOML configuration file. You cannot use it with `--photos-dir`. |
| `--combined` | off | Also make a combined video of the 2 config subjects. Needs `--config`. |
| `--combined-only` | off | Make only the combined video. Needs `--config`. |
| `--output-dir` | `./output` | Where to write the output video |
| `--seconds-per-photo` | `2` | How long each photo is shown |
| `--max-days` | *(photo count)* | Number of days to cover |
| `--crf` | `23` | H.265 quality — lower = better quality, larger file (18–28 is typical) |
| `--resolution` | `1920x1080` | Output resolution |
| `--subtitle` | *(derived from `--max-days`)* | Title card subtitle, e.g. `"First year"` |
| `--start-date` | *(inferred from earliest photo)* | Override start date (`YYYY-MM-DD`). The first photo shows "Day 0". |
