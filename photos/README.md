# photos/

Place your dated photos here, one subdirectory per subject:

```
photos/
    Emma/
        2024-01-15.jpg    <- earliest photo becomes day 1
        2024-01-16.jpg
        2024-01-18.jpg    <- gaps are fine; last known photo is reused
        ...
```

**File naming:** `YYYY-MM-DD.<ext>` — supported formats are `.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`.

**Starting from camera-generated filenames?** Use `preprocess.py` to copy and rename them automatically:

```bash
uv run preprocess.py --input-dir ./img/Emma --output-dir ./photos/Emma
```

See the project README for full preprocessing options.
