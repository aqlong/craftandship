# Stock photo placeholders

The images in this directory are generated placeholders: a soft two-color
gradient per category with the category name drawn on it. The original photos
used in production were removed from this public copy because their license
does not clearly allow redistribution and some of them showed real people.

- Filenames, extensions, and `manifest.json` entries are unchanged in shape, so
  the v1 pipeline (`cli/build.py`) and the v2 selector (`compose/stock_photos.py`)
  behave as they did. Only the pixels are different.
- `manifest.json` lists more entries than there are files on disk. Entries
  without a file are dormant and skipped at build time.
- To fetch a fresh, properly licensed set, run `scripts/curate_stock_photos.py`
  with your own `PIXABAY_API_KEY` (see the script's docstring). It resizes the
  downloads and updates `manifest.json`.
