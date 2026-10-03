# photos/

Put your dated photos here, in one folder for each subject. Git ignores the subject folders.

```
photos/
    Emma/
        2024-01-15.jpg    <- earliest photo, shown as "Day 0"
        2024-01-16.jpg
        2024-01-18.jpg    <- a missing day is fine: the video shows the last photo again
        ...
```

Name each photo `YYYY-MM-DD.<ext>`. The supported extensions are `.jpg`, `.jpeg`, `.png`, `.heic` and `.heif`.

If your photos have camera file names, put the originals in `raw/<name>/`. Then use `preprocess.py` to copy them here
with dated names:

```bash
uv run preprocess.py --input-dir ./raw/Emma --output-dir ./photos/Emma
```

The project README describes all preprocess options.
