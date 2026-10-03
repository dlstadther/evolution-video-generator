# output/

`evolution.py` writes its videos and summary images here. Git ignores these files.

| Input | Output file |
|---|---|
| One subject, for example the folder `photos/Emma` | `evolution_Emma.mp4` |
| Two subjects in a configuration file, Emma on the left and Noah on the right | `evolution_Emma_Noah_combined.mp4` |
| One subject with `--summary-image` | `summary_Emma.png` |
| Two subjects with `--summary-image` | `summary_Emma_Noah_combined.png` |

The subject name is the photo folder name, unless the configuration file sets `name`.

To make a video:

```bash
uv run evolution.py --photos-dir ./photos/Emma
```

The project README describes all options.
