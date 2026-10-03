# evolution-video

`evolution.py` makes an "Evolution of \<name\>" video from a folder of daily photos. The video has three parts:

1. A title card, for example "Evolution of Emma" with the subtitle "First 6 months".
2. One clip for each day. Each clip shows that day's photo and an age label, for example "Day 0", "3 weeks 2 days"
   or "6 months".
3. A summary slide that shows the first and last photos side by side, with their ages.

The script can also put two subjects side by side in one video. See
[Combined side-by-side video](#combined-side-by-side-video).

## Requirements

- [uv](https://docs.astral.sh/uv/). It runs both scripts and installs their Python packages for you.
- [ffmpeg](https://ffmpeg.org/) with `libfreetype`. On macOS, run `brew install ffmpeg-full`. The standard `ffmpeg`
  package has no `libfreetype`, and the script needs it to draw text.

## Project folders

```
evolution-video/
├── evolution.py        ← makes the video
├── preprocess.py       ← renames raw photos to YYYY-MM-DD.<ext>
├── examples/           ← sample configuration files
│   ├── single.toml
│   └── combined.toml
├── raw/                ← your original photos, one folder per subject (git ignores it)
│   └── Emma/
├── photos/             ← dated photos, one folder per subject (git ignores the subject folders)
│   └── Emma/
│       ├── 2024-01-15.jpg
│       └── 2024-01-16.heic
├── output/             ← videos and summary images (git ignores the files)
└── emma.toml           ← your own configuration files (git ignores *.toml at the root)
```

The photos and videos are personal, so git ignores them. `photos/` and `output/` each keep a README file only.

## Workflow

```
raw/Emma/              (camera file names, for example PXL_20240115_093012.jpg)
    │
    ▼
preprocess.py          renames copies to YYYY-MM-DD.<ext>
    │
    ▼
photos/Emma/           (dated photos, for example 2024-01-15.jpg)
    │
    ▼
evolution.py           makes the video
    │
    ▼
output/evolution_Emma.mp4
```

## Step 1: Prepare dated photos

`evolution.py` reads photos named `YYYY-MM-DD.<ext>`. The supported extensions are `.jpg`, `.jpeg`, `.png`, `.heic`
and `.heif`. The script ignores other file types. If a supported file has a name that is not a date, the script skips
it and shows a warning.

If your photos already have dated names, put them in `photos/<name>/` and go to step 2.

If your photos have camera file names, use `preprocess.py`. It copies each photo from the input folder to the output
folder with a dated name. It does not change the original files.

```bash
# Show what the script will copy. This writes no files.
uv run preprocess.py --input-dir ./raw/Emma --output-dir ./photos/Emma --dry-run

# Copy and rename the photos
uv run preprocess.py --input-dir ./raw/Emma --output-dir ./photos/Emma

# Set the timezone for photos that get their date from the file modification time
uv run preprocess.py --input-dir ./raw/Emma --output-dir ./photos/Emma --timezone America/New_York
```

`preprocess.py` finds the date of each photo in this order:

1. The date in the file name, for example `PXL_20240115_093012.jpg` or `IMG_20240115_093012.jpg`. Camera apps write
   local time into the file name.
2. The EXIF date. EXIF is the photo data that the camera stores in the file. The script uses it when the file name has
   no date, for example `IMG_1234.HEIC`.
3. The file modification time. The script converts it to a date with `--timezone`.

If two or more photos get the same date, the script copies none of them. It lists these conflicts at the end, so that
you can choose one photo and rename it yourself.

### Preprocess options

| Flag | Default | Description |
|---|---|---|
| `--input-dir` | *(required)* | Folder with the original photos |
| `--output-dir` | *(required)* | Folder for the renamed copies |
| `--timezone` | `UTC` | IANA timezone for dates from the file modification time only, for example `America/New_York`. Dates from the file name and EXIF are already in local time. |
| `--subject` | *(input folder name)* | Name to show in the log output |
| `--dry-run` | off | Show what the script will copy, but write no files |

## Step 2: Make the video

```bash
# The start date is the date of the earliest photo
uv run evolution.py --photos-dir ./photos/Emma

# Set the start date yourself
uv run evolution.py --photos-dir ./photos/Emma --start-date 2024-01-15
```

The script writes `output/evolution_Emma.mp4`. The video uses the folder name, `Emma`, as the subject name.

Rules for the days in the video:

- The start date is "Day 0". The age labels count from the start date.
- By default, the video has one day for each photo. To set a different number of days, use `--max-days`.
- If a day has no photo, the video shows the last photo again with that day's age label.

## Configuration file

You can put the configuration in a TOML file and pass it with `--config`. The number of `[[subjects]]` tables selects
the video type:

- 1 subject: the script makes a single video for that subject.
- 2 subjects: the script makes only a combined side-by-side video.
- Any other number of subjects is an error.

To start, copy a sample file to the repository root:

```bash
cp examples/single.toml emma.toml
```

In the copy, change the subject name and the photo folder. Also change each path that starts with `../` to start with
`./`, because the copy is now at the repository root. Then make the video:

```bash
uv run evolution.py --config emma.toml
```

[`examples/single.toml`](examples/single.toml) shows all keys with their default values. This is the minimum file:

```toml
[[subjects]]
photos_dir = "./photos/Emma"
```

Rules:

- Top-level keys: `output_dir`, `seconds_per_photo`, `max_days`, `crf`, `resolution`, `subtitle` and `workers`.
- Subject keys: `photos_dir` (required), `name` and `start_date`.
- A top-level key inside `[[subjects]]` is an error. An unknown key is also an error.
- A CLI flag overrides the configuration value. `--start-date` sets the start date of every subject.
- Relative paths resolve against the folder that contains the configuration file. A file in `examples/` uses
  `../photos/...`, and a file at the repository root uses `./photos/...`.

## Combined side-by-side video

A combined video shows two subjects side by side, day by day. Use a configuration file with two `[[subjects]]` tables,
for example [`examples/combined.toml`](examples/combined.toml).

The first subject is the left panel, and the second subject is the right panel. The order also sets the name order in
the title card, the output file name and the summary slide columns. To swap the panels, swap the two `[[subjects]]`
tables.

```toml
[[subjects]]                    # first subject = left panel
photos_dir = "./photos/Emma"

[[subjects]]                    # second subject = right panel
photos_dir = "./photos/Noah"
```

The combined video:

- Starts with the title card "Evolution of Emma & Noah".
- Shows each subject's name at the top of its panel and its age at the bottom. Day N of each panel counts from that
  subject's own start date.
- Covers the larger photo count of the two subjects. The subject with fewer photos repeats its last photo until the
  end. To set a different length, use `max_days` or `--max-days`.
- Ends with a 2x2 summary slide. The columns are the subjects. The top row shows the first photos, and the bottom row
  shows the last photos.
- Is written to `output/evolution_<left>_<right>_combined.mp4`.

To make a single video for each subject too, use a separate 1-subject configuration file for each subject.

## Summary image

To get only the summary slide as a PNG image, add `--summary-image`. The script makes no video, so this is fast.

```bash
uv run evolution.py --photos-dir ./photos/Emma --summary-image
```

The script writes `output/summary_Emma.png`. A combined configuration file gives
`output/summary_<left>_<right>_combined.png`, with the 2x2 grid.

## Options

| Flag | Default | Description |
|---|---|---|
| `--photos-dir` | *(required unless `--config`)* | Folder with the dated photos of one subject |
| `--config` | *(none)* | TOML configuration file with 1 subject (single video) or 2 subjects (combined video). You cannot use it with `--photos-dir`. |
| `--output-dir` | `./output` | Folder for the output video or image |
| `--seconds-per-photo` | `2` | Number of seconds that the video shows each photo |
| `--max-days` | *(photo count)* | Number of days in the video. A combined video uses the larger photo count of the two subjects. |
| `--crf` | `23` | H.265 quality. A lower value gives better quality and a larger file. Values from 18 to 28 are typical. |
| `--resolution` | `1920x1080` | Output resolution, as `WIDTHxHEIGHT` |
| `--subtitle` | *(from `--max-days`)* | Title card subtitle, for example `"First year"` |
| `--start-date` | *(earliest photo)* | Start date (`YYYY-MM-DD`) of every subject. The photo on this date shows "Day 0". |
| `--summary-image` | off | Write only the summary slide as a PNG image. The script makes no video. |
| `--workers` | *(CPU count, at most 8)* | Number of clips that ffmpeg renders at the same time. Use `1` to render one clip at a time. |
