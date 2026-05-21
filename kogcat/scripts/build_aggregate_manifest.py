"""Aggregate per-target om-core manifests into a single release-asset JSON.

Per-target manifests (`bin/om-core-<target>/manifest.json`) are written by
`release_manifest.py` during the matrix build. They are git-tracked and the
canonical source for the in-tree CC plugin bootstrap (it reads them by path).

External clients that ship as marketplace plugins (obsidian-kogcat in
particular) cannot bundle these per-target files at install time — the
binaries themselves don't ship with the plugin, and the per-target manifest
URLs would have to be fetched one-by-one. This aggregator collapses the N
manifests into one file uploaded as a GH Release asset, so a marketplace
client can do a single fetch to learn:

    • which om-core version this release ships
    • for every supported target: download URL + sha256 + size

Why aggregate as a release asset (vs. raw.githubusercontent.com on main):
    • Tag-pinned. Release assets are immutable per release; main can move.
    • Single host. No CDN-cache divergence between manifest and binary.

Schema (schema_version=2):
    {
      "schema_version": 2,
      "om_core_version": "0.36.0",
      "spec": "19",
      "targets": {
        "<rust-target-triple>": {
          "asset_name": "om-core-bin-<triple>.tar.xz",
          "url": "https://.../releases/download/v.../om-core-bin-<triple>.tar.xz",
          "sha256": "<hex>",
          "size_bytes": <int>,
          "format": "tar.xz"
        },
        ...
      }
    }

`format` ("tar.xz" → onedir bundle archive the client extracts; absent →
legacy raw single-file binary) was added in schema 2.

Usage (CI):
    python scripts/build_aggregate_manifest.py \
        --inputs manifests/manifest-aarch64-apple-darwin/manifest.json \
                 manifests/manifest-x86_64-apple-darwin/manifest.json \
                 manifests/manifest-x86_64-pc-windows-msvc/manifest.json \
        --version 0.16.0 \
        --output aggregate-manifest.json

Inputs are the per-target manifests written by `release_manifest.py`. The
aggregator validates that every input declares the same version and `spec`
field — divergence indicates a mismatched matrix run and fails the build.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 2


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", nargs="+", required=True, type=Path,
                    help="per-target manifest.json paths")
    ap.add_argument("--version", required=True,
                    help="expected om-core version; mismatched inputs fail the build")
    ap.add_argument("--output", required=True, type=Path,
                    help="aggregate manifest output path")
    args = ap.parse_args()

    targets: dict[str, dict] = {}
    spec_value: str | None = None

    for p in args.inputs:
        m = load_manifest(p)
        target = m.get("target")
        version = m.get("version")
        spec = m.get("spec")
        if not target:
            raise SystemExit(f"{p}: missing 'target' field")
        if version != args.version:
            raise SystemExit(
                f"{p}: version mismatch — got {version!r}, expected {args.version!r}. "
                "Re-run the matrix with a single consistent --version input."
            )
        if spec_value is None:
            spec_value = spec
        elif spec != spec_value:
            raise SystemExit(
                f"{p}: spec mismatch — got {spec!r}, earlier inputs had {spec_value!r}"
            )
        if target in targets:
            raise SystemExit(f"duplicate target {target!r} across inputs")

        url = m.get("om_core_bin_url") or ""
        # Asset name = last URL segment. release_manifest.py shapes the URL
        # as .../releases/download/vX/asset_name, so the basename is exact.
        asset_name = url.rsplit("/", 1)[-1] if url else ""

        targets[target] = {
            "asset_name": asset_name,
            "url": url,
            "sha256": m.get("om_core_bin_sha256", ""),
            "size_bytes": int(m.get("size_bytes", 0)),
            "format": m.get("format", "raw"),
        }

    aggregate = {
        "schema_version": SCHEMA_VERSION,
        "om_core_version": args.version,
        "spec": spec_value or "",
        "targets": targets,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")

    print(f"aggregate manifest written: {args.output}")
    print(f"  schema_version: {SCHEMA_VERSION}")
    print(f"  om_core_version: {args.version}")
    print(f"  spec: {spec_value}")
    print(f"  targets: {sorted(targets.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
