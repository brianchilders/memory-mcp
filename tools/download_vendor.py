#!/usr/bin/env python3
"""
tools/download_vendor.py — Download (or re-download) all vendored frontend assets.

memory-mcp serves Bootstrap, HTMX, and vis-network from static/vendor/ instead
of CDN URLs to eliminate supply-chain risk from third-party script injection.

This script is the single source of truth for:
  - which asset is at which URL
  - the pinned version of each asset
  - where it is stored on disk

Run this script whenever you need to update a library version:
    python tools/download_vendor.py

It will download every asset listed in ASSETS, overwriting any existing file.
A manifest is written to static/vendor/manifest.json for audit purposes.

Usage:
    python tools/download_vendor.py            # re-download all
    python tools/download_vendor.py --check    # verify files exist without downloading
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Asset registry ─────────────────────────────────────────────────────────────
# Each entry: (local_path_relative_to_vendor_dir, source_url, version_tag)
ASSETS: list[tuple[str, str, str]] = [
    (
        "css/bootstrap.min.css",
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
        "5.3.3",
    ),
    (
        "js/bootstrap.bundle.min.js",
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
        "5.3.3",
    ),
    (
        "js/htmx.min.js",
        "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js",
        "1.9.12",
    ),
    (
        "js/vis-network.min.js",
        "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js",
        "9.1.9",
    ),
]

VENDOR_DIR = Path(__file__).parent.parent / "static" / "vendor"
MANIFEST_PATH = VENDOR_DIR / "manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def download(rel_path: str, url: str) -> Path:
    dest = VENDOR_DIR / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading {url}")
    urllib.request.urlretrieve(url, dest)  # noqa: S310 (controlled URL list above)
    return dest


def check_mode() -> bool:
    """Return True if all files are present; print missing files and return False otherwise."""
    missing = []
    for rel_path, _url, _ver in ASSETS:
        if not (VENDOR_DIR / rel_path).exists():
            missing.append(rel_path)
    if missing:
        print("Missing vendor files:")
        for m in missing:
            print(f"  static/vendor/{m}")
        print("\nRun `python tools/download_vendor.py` to download them.")
        return False
    print(f"All {len(ASSETS)} vendor files present.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only verify files exist; exit 1 if any are missing")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check_mode() else 1)

    manifest: list[dict] = []
    for rel_path, url, version in ASSETS:
        dest = download(rel_path, url)
        size = dest.stat().st_size
        digest = sha256(dest)
        manifest.append({
            "file":        rel_path,
            "source_url":  url,
            "version":     version,
            "size_bytes":  size,
            "sha256":      digest,
            "downloaded":  datetime.now(timezone.utc).isoformat(),
        })
        print(f"  -> static/vendor/{rel_path} ({size:,} bytes, sha256={digest[:16]}...)")

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nManifest written to static/vendor/manifest.json ({len(manifest)} entries).")


if __name__ == "__main__":
    main()
