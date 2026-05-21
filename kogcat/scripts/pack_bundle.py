"""Pack a PyInstaller onedir bundle into a `.tar.xz` release asset.

The onedir build produces `dist/om-core-bin/` (the `om-core-bin`
executable plus `_internal/`). This packs that directory's contents at
the archive root — so the fetcher (`hooks/_bin_fetch_runner.py`) extracts
straight into the per-version cache bundle dir with no wrapping level.

xz: `lzma` + `tarfile`'s `w:xz` / `r:xz` are stdlib (Python 3.3+), so the
producer here and the fetcher share one codec with zero extra deps, and
xz's ratio keeps the download asset small.

Usage (CI):
    python scripts/pack_bundle.py \
        --bundle om-core/dist/om-core-bin \
        --output release-assets/om-core-bin-<target>.tar.xz
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True, type=Path,
                    help="PyInstaller onedir bundle directory")
    ap.add_argument("--output", required=True, type=Path,
                    help="output .tar.xz path")
    args = ap.parse_args()

    if not args.bundle.is_dir():
        print(f"error: bundle dir not found: {args.bundle}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Members land at the archive root (no wrapping dir) so the fetcher
    # extracts straight into the cache bundle dir. Sorted top-level entries
    # for a stable member order across rebuilds.
    with tarfile.open(args.output, "w:xz") as tf:
        for name in sorted(os.listdir(args.bundle)):
            tf.add(args.bundle / name, arcname=name)

    size = args.output.stat().st_size
    print(f"packed {args.output} ({size} bytes, {size / (1 << 20):.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
