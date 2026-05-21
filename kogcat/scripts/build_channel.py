"""Merge a freshly-built om-core release into the rolling channel.json.

Background — why a channel exists
=================================
Per-target manifests (`bin/om-core-<target>/manifest.json`) and the
single-version `aggregate-manifest.json` both pin **one exact om-core
version**. A client that wants a newer binary must ship a new plugin
release that bumps those pins — binary upgrades are chained to client
release cadence.

`channel.json` breaks that chain. It is a *rolling, mutable index* of every
om-core release still being served, published as an asset on a fixed
`channel` release (not a `v<version>` tag). A client fetches it once at
startup, filters the `releases[]` array by its own compatibility floor
(series + spec), and picks the highest version it can run — no plugin
release needed to move the binary forward.

Maintenance model
=================
The release workflow calls this script during `commit-manifests`:

  1. download the current `channel.json` from the `channel` release
     (absent on the very first run — handled as an empty channel)
  2. run this script: merge the just-built version's per-target manifests
     into `releases[]`, re-sort, trim to `--keep` newest
  3. upload the result back to the `channel` release (`--clobber`)

The `--keep` window is the single source of truth for the binary host's
retention: `prune-releases` derives its keep-set from `channel.json`'s
`releases[].om_core_version`, so a release drops off the channel and
becomes prune-eligible in the same step. Keep the window wide enough to
cover any client lag plausible between two releases.

Schema (schema_version=3)
=========================
    {
      "schema_version": 3,
      "updated_at": "2026-05-15T11:22:31Z",
      "releases": [
        {
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
        },
        ...
      ]
    }

`format` ("tar.xz" → onedir bundle archive the client extracts; absent
on a legacy entry → raw single-file binary) was added in schema 3. A
client gating on `schema_version` rejects an unrecognised channel and
falls back to its bundled manifest rather than mis-consuming an asset.

`releases[]` is sorted newest-first by semver. `spec` is the om-core
`SCHEMA_VERSION` carried verbatim from the per-target manifests; clients
that gate on a schema break filter on it. Per-release `api_minor` is
intentionally omitted — the runtime `/v1/capabilities` probe is the
authoritative api_minor gate; the channel filter anchors on series + spec.

Usage (CI):
    python scripts/build_channel.py \
        --existing channel-in/channel.json \
        --inputs manifests/manifest-aarch64-apple-darwin/manifest.json \
                 manifests/manifest-x86_64-apple-darwin/manifest.json \
                 manifests/manifest-x86_64-pc-windows-msvc/manifest.json \
        --version 0.35.3 \
        --keep 6 \
        --output channel-out/channel.json

`--existing` may point at a missing path: a first-ever run starts from an
empty channel rather than failing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3
DEFAULT_KEEP = 6


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _semver_key(version: str) -> tuple[int, int, int]:
    """Sort key for an `X.Y.Z` version. Pre-release suffixes are stripped
    (`3-rc1` → `3`); non-numeric or short versions degrade to 0-padding
    rather than raising — channel sorting must never crash a release."""
    nums: list[int] = []
    for part in version.split(".")[:3]:
        head = part.split("-", 1)[0]
        nums.append(int(head) if head.isdigit() else 0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _load_existing(path: Path | None) -> dict:
    """Load an existing channel.json, or return an empty channel.

    A missing path is the expected first-run state, not an error. A present
    but malformed file *is* an error — silently discarding a corrupt channel
    would orphan every release it used to list (prune derives its keep-set
    from here)."""
    if path is None or not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "releases": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"error: existing channel unreadable ({path}): {e}")
    if not isinstance(data.get("releases"), list):
        raise SystemExit(
            f"error: existing channel malformed — no 'releases' array: {path}"
        )
    return data


def _build_release_entry(inputs: list[Path], version: str) -> dict:
    """Assemble one `releases[]` entry from this version's per-target
    manifests, asserting they agree on version and spec."""
    targets: dict[str, dict] = {}
    spec_value: str | None = None

    for p in inputs:
        if not p.is_file():
            raise SystemExit(f"error: manifest not found: {p}")
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"error: manifest unreadable ({p}): {e}")

        target = m.get("target")
        m_version = m.get("version")
        spec = m.get("spec")
        if not target:
            raise SystemExit(f"{p}: missing 'target' field")
        if m_version != version:
            raise SystemExit(
                f"{p}: version mismatch — got {m_version!r}, expected {version!r}. "
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

    return {
        "om_core_version": version,
        "spec": spec_value or "",
        "targets": targets,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--existing", type=Path, default=None,
                    help="current channel.json (may be missing on first run)")
    ap.add_argument("--inputs", nargs="+", required=True, type=Path,
                    help="this version's per-target manifest.json paths")
    ap.add_argument("--version", required=True,
                    help="om-core version being merged in (no leading 'v')")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"retain the N newest releases (default {DEFAULT_KEEP})")
    ap.add_argument("--output", required=True, type=Path,
                    help="channel.json output path")
    args = ap.parse_args()

    if args.keep < 1:
        raise SystemExit(f"error: --keep must be >= 1, got {args.keep}")

    existing = _load_existing(args.existing)
    new_entry = _build_release_entry(args.inputs, args.version)

    # Merge: a re-run of the same version replaces the prior entry (e.g. a
    # clobbered rebuild) rather than duplicating it.
    releases = [
        r for r in existing["releases"]
        if r.get("om_core_version") != args.version
    ]
    releases.append(new_entry)

    releases.sort(
        key=lambda r: _semver_key(str(r.get("om_core_version", "0"))),
        reverse=True,
    )
    kept = releases[:args.keep]
    dropped = releases[args.keep:]

    channel = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now_iso(),
        "releases": kept,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(channel, indent=2) + "\n", encoding="utf-8",
    )

    print(f"channel written: {args.output}")
    print(f"  schema_version: {SCHEMA_VERSION}")
    print(f"  merged version: {args.version} (spec {new_entry['spec']})")
    print(f"  releases kept ({len(kept)}): "
          f"{[r['om_core_version'] for r in kept]}")
    if dropped:
        print(f"  dropped beyond --keep {args.keep}: "
              f"{[r['om_core_version'] for r in dropped]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
