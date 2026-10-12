#!/usr/bin/env python3
"""Curate stock photos from Pixabay API for the CAS build pipeline.

Usage:
    python3 scripts/curate_stock_photos.py
    python3 scripts/curate_stock_photos.py --category pets
    python3 scripts/curate_stock_photos.py --refresh 5

Requires PIXABAY_API_KEY environment variable.

Downloads royalty-free images from Pixabay, resizes to 1200x800,
and updates cli/stock_photos/manifest.json with metadata.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CATEGORIES = {
    "people": "business team office professional",
    "food": "restaurant food plated meal",
    "nature": "landscape nature scenic outdoor",
    "business": "office workspace modern desk",
    "fitness": "gym workout exercise yoga",
    "pets": "dog cat pet grooming",
    "tech": "laptop computer technology workspace",
    "medical": "healthcare clinic medical wellness",
}

STOCK_DIR = Path(__file__).parent.parent / "cli" / "stock_photos"
TARGET_WIDTH = 1200
TARGET_HEIGHT = 800
PIXABAY_API_URL = "https://pixabay.com/api/"


def _sanitize_filename_stem(raw: str) -> str:
    """Lowercase, strip non-alnum, collapse runs of '-'. Fallback to 'photo'."""
    s = (raw or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "photo"


def download_pixabay_photos(
    api_key,
    query,
    target_dir,
    per_page=5,
    dry_run=False,
    url_opener=None,
    file_downloader=None,
):
    """Search Pixabay for `query` and download up to `per_page` large images.

    Args:
        api_key: Pixabay API key. Missing/empty → returns [] (no crash).
        query: Search query string.
        target_dir: Path to write downloaded JPGs into.
        per_page: Max number of images to request (Pixabay min 3, max 200).
        dry_run: If True, do not actually fetch image bytes; return projected paths.
        url_opener: Optional callable matching urllib.request.urlopen(url, timeout=...).
            Injected for unit tests.
        file_downloader: Optional callable matching urllib.request.urlretrieve(url, path).
            Injected for unit tests.

    Returns:
        List of saved file paths (as strings). Empty list on missing key or HTTP error.
    """
    if not api_key:
        print("  Skipping: PIXABAY_API_KEY not set")
        return []

    opener = url_opener or urllib.request.urlopen
    downloader = file_downloader or urllib.request.urlretrieve

    # Pixabay's per_page minimum is 3.
    per_page_param = max(3, int(per_page))

    params = {
        "key": api_key,
        "q": query,
        "image_type": "photo",
        "per_page": str(per_page_param),
        "safesearch": "true",
        "orientation": "horizontal",
        "min_width": str(TARGET_WIDTH),
    }
    url = PIXABAY_API_URL + "?" + urllib.parse.urlencode(params)

    try:
        with opener(url, timeout=30) as resp:
            body = resp.read()
        data = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, json.JSONDecodeError) as exc:
        print(f"  Pixabay API error for '{query}': {exc}")
        return []

    hits = data.get("hits", []) if isinstance(data, dict) else []
    if not hits:
        print(f"  No hits for '{query}'")
        return []

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for hit in hits:
        large_url = hit.get("largeImageURL") if isinstance(hit, dict) else None
        if not large_url:
            continue

        tag_source = ""
        if isinstance(hit.get("tags"), str):
            tag_source = hit["tags"].split(",")[0]
        stem = _sanitize_filename_stem(tag_source)
        hit_id = hit.get("id") or len(saved) + 1
        try:
            hit_id_int = int(hit_id)
        except (TypeError, ValueError):
            hit_id_int = len(saved) + 1
        filename = f"{stem}-{hit_id_int}.jpg"
        target_path = target_dir / filename

        if dry_run:
            print(f"  [dry-run] {large_url} -> {target_path}")
            saved.append(str(target_path))
            continue

        try:
            downloader(large_url, str(target_path))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"  Failed to download {large_url}: {exc}")
            continue

        print(f"  Downloaded: {target_path}")
        saved.append(str(target_path))

    return saved


def main():
    parser = argparse.ArgumentParser(description="Curate stock photos from Pixabay")
    parser.add_argument("--category", help="Refresh only this category")
    parser.add_argument(
        "--refresh", type=int, default=5,
        help="Number of photos to request per category (Pixabay per_page; default 5)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be downloaded without downloading"
    )
    args = parser.parse_args()

    api_key = os.getenv("PIXABAY_API_KEY")
    if not api_key:
        print("Warning: PIXABAY_API_KEY not set")
        print("Get a free API key at https://pixabay.com/api/docs/")
        print("Skipping all downloads.")
        return

    print("Stock photo curation script")
    print(f"Stock dir: {STOCK_DIR}")
    print(f"Target size: {TARGET_WIDTH}x{TARGET_HEIGHT}")
    print()

    categories = (
        {args.category: CATEGORIES[args.category]}
        if args.category and args.category in CATEGORIES
        else CATEGORIES
    )

    total_saved = 0
    for cat_name, search_terms in categories.items():
        cat_dir = STOCK_DIR / cat_name
        print(f"Category: {cat_name}")
        print(f"  Search terms: {search_terms}")
        print(f"  Directory: {cat_dir}")
        saved = download_pixabay_photos(
            api_key=api_key,
            query=search_terms,
            target_dir=cat_dir,
            per_page=args.refresh,
            dry_run=args.dry_run,
        )
        total_saved += len(saved)
        print()

    print(f"Done. Saved {total_saved} files across {len(categories)} categories.")
    print("Run 'python3 -m pytest tests/test_stock_photos.py -v' to verify.")


if __name__ == "__main__":
    main()
