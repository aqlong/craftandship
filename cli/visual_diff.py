"""Pixel-diff two sets of page screenshots for visual regression testing.

Uses Pillow for image loading and per-pixel comparison.
No numpy dependency required.

Usage:
    from cli.visual_diff import compare_screenshots
    result = compare_screenshots('/tmp/baseline/', '/tmp/candidate/', '/tmp/diff/')
    if not result['passed']:
        for page in result['pages']:
            if page['mismatch_pct'] > 0.02:
                print(f"Regression on {page['page']}: {page['mismatch_pct']:.1%}")
"""
from pathlib import Path


def compare_screenshots(
    baseline_dir: str,
    candidate_dir: str,
    output_dir: str,
    threshold: float = 0.02,
) -> dict:
    """Pixel-diff two sets of page screenshots.

    Args:
        baseline_dir: Directory containing baseline PNG screenshots
        candidate_dir: Directory containing candidate PNG screenshots
        output_dir: Directory to write diff images
        threshold: Maximum allowed mismatch percentage (0.0-1.0)

    Returns:
        {"passed": bool, "pages": [{"page", "mismatch_pct", "diff_path"}]}
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return {
            "passed": False,
            "pages": [{"page": "setup", "mismatch_pct": 1.0, "diff_path": None,
                        "error": "Pillow not installed. Run: pip install Pillow"}],
        }

    baseline_path = Path(baseline_dir)
    candidate_path = Path(candidate_dir)
    diff_path = Path(output_dir)
    diff_path.mkdir(parents=True, exist_ok=True)

    results = []

    # Find all PNG files in baseline
    baseline_files = sorted(baseline_path.glob("*.png"))
    if not baseline_files:
        return {
            "passed": False,
            "pages": [{"page": "setup", "mismatch_pct": 1.0, "diff_path": None,
                        "error": f"No PNG files found in {baseline_dir}"}],
        }

    for baseline_file in baseline_files:
        page_name = baseline_file.stem
        candidate_file = candidate_path / baseline_file.name

        if not candidate_file.exists():
            results.append({
                "page": page_name,
                "mismatch_pct": 1.0,
                "diff_path": None,
                "error": f"Missing candidate: {candidate_file.name}",
            })
            continue

        try:
            img_a = Image.open(baseline_file).convert("RGB")
            img_b = Image.open(candidate_file).convert("RGB")

            # Resize candidate to match baseline if dimensions differ
            if img_a.size != img_b.size:
                img_b = img_b.resize(img_a.size, Image.LANCZOS)

            # Per-pixel difference
            diff_img = ImageChops.difference(img_a, img_b)

            # Count mismatched pixels (any channel differs by more than 10)
            total_pixels = img_a.size[0] * img_a.size[1]
            mismatch_count = 0
            diff_data = (diff_img.get_flattened_data() if hasattr(diff_img, 'get_flattened_data')
                         else diff_img.getdata())
            for pixel in diff_data:
                if max(pixel) > 10:  # tolerance for minor rendering differences
                    mismatch_count += 1

            mismatch_pct = mismatch_count / total_pixels if total_pixels > 0 else 0

            # Write diff image (amplified for visibility)
            diff_output = diff_path / f"{page_name}_diff.png"
            # Amplify differences: multiply by 10 for visibility
            amplified = diff_img.point(lambda x: min(x * 10, 255))
            amplified.save(str(diff_output))

            results.append({
                "page": page_name,
                "mismatch_pct": mismatch_pct,
                "diff_path": str(diff_output),
            })

        except Exception as e:
            results.append({
                "page": page_name,
                "mismatch_pct": 1.0,
                "diff_path": None,
                "error": str(e),
            })

    passed = all(r.get("mismatch_pct", 1.0) <= threshold for r in results)
    return {"passed": passed, "pages": results}
