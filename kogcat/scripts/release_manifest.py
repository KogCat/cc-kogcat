"""Generate manifest.json for an om-core binary release.

The manifest's ``spec`` field is derived from the om-core source tree's
``SCHEMA_VERSION`` constant (``om_core/kb/schema.py``). Hardcoding it here
silently desynced the public-binary spec advertisement from the engine after
om-core's spec bumps — clients gated on ``spec`` keep refusing valid binaries
or accepting invalid ones. The pipeline now refuses to emit a manifest if it
cannot read the constant from a checked-out om-core source tree.

Called by GH Actions release workflow after PyInstaller build. `--binary`
points at the staged release asset — the onedir bundle packed as
`om-core-bin-<target>.tar.xz`. Its filename becomes the manifest's asset
name verbatim, and `--format` records how a client should consume it
(`tar.xz` → extract the onedir bundle; `raw` → legacy single-file binary).

Repository inference (no hardcoded fallback):
  Reads `GITHUB_REPOSITORY` (`owner/name`) from the workflow env — the
  same string GitHub Actions exports on every runner. The manifest
  `om_core_bin_url` then automatically points at *whichever repo this
  workflow is running in*: the public mirror for ship builds, the
  private dev fork for in-team test releases. No env override needed.
  CLI flags `--gh-org` / `--gh-repo` are still accepted for local
  manifest regeneration outside CI; if both env and flags are missing
  we fail loudly (better than silently emitting a wrong URL).

    # macOS / Linux (CI path — repo inferred from GITHUB_REPOSITORY)
    python scripts/release_manifest.py \
        --binary dist/om-core-bin \
        --target aarch64-apple-darwin \
        --version 0.13.0 \
        --output bin/om-core-aarch64-apple-darwin/manifest.json

    # Local manifest regeneration outside CI
    python scripts/release_manifest.py \
        --binary /path/to/om-core-bin \
        --target aarch64-apple-darwin --version 0.13.1 \
        --gh-org <org> --gh-repo <repo>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


_SCHEMA_VERSION_RE = re.compile(r"^\s*SCHEMA_VERSION\s*=\s*(\d+)\s*$", re.MULTILINE)


def _read_spec_from_om_core(src_root: Path) -> str:
    schema = src_root / "om_core" / "kb" / "schema.py"
    if not schema.is_file():
        raise SystemExit(
            f"error: om-core schema source not found: {schema}. "
            "Pass --om-core-src <path-to-checked-out-om-core-repo>."
        )
    m = _SCHEMA_VERSION_RE.search(schema.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(
            f"error: SCHEMA_VERSION constant not parseable from {schema}"
        )
    return m.group(1)


def _infer_repo() -> tuple[str, str] | None:
    """Read `owner/name` from GitHub Actions env. Returns None if absent."""
    raw = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not raw or "/" not in raw:
        return None
    owner, _, name = raw.partition("/")
    if not owner or not name:
        return None
    return owner, name


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--binary", required=True, type=Path,
                    help="path to built om-core-bin")
    ap.add_argument("--target", required=True,
                    help="target triple, e.g. aarch64-apple-darwin")
    ap.add_argument("--version", required=True,
                    help="release version, e.g. 0.13.0 (no leading 'v')")
    ap.add_argument("--output", type=Path,
                    help="manifest.json output path (default: "
                         "bin/om-core-<target>/manifest.json)")
    ap.add_argument("--om-core-src", type=Path, default=None,
                    help="path to a checked-out om-core source tree. The "
                         "manifest's `spec` field is read from "
                         "<src>/om_core/kb/schema.py SCHEMA_VERSION. "
                         "Defaults to the sibling `om-core/` dir relative "
                         "to this script's plugin root (workflow layout). "
                         "Override with OM_CORE_SRC env var.")
    ap.add_argument("--build-flags",
                    default="fastembed+sqlite-vec bundled",
                    help="build provenance string written into manifest. "
                         "Pass an empty string ('') to omit the field entirely.")
    ap.add_argument("--build-id",
                    default=None,
                    help="opaque build identifier (e.g. om-core git short "
                         "SHA) written into manifest. Unlike build_flags "
                         "this is safe to expose: it identifies a build "
                         "for issue-triage without describing internals.")
    ap.add_argument("--gh-org",
                    default=None,
                    help="GitHub org/owner for the release URL "
                         "(default: parsed from GITHUB_REPOSITORY env)")
    ap.add_argument("--gh-repo",
                    default=None,
                    help="GitHub repo name for the release URL "
                         "(default: parsed from GITHUB_REPOSITORY env)")
    ap.add_argument("--format", default="tar.xz",
                    help="asset format recorded in the manifest: 'tar.xz' "
                         "(onedir bundle archive, default) or 'raw' "
                         "(legacy single-file binary)")
    args = ap.parse_args()

    if not args.binary.is_file():
        print(f"error: binary not found: {args.binary}", file=sys.stderr)
        return 1

    # Repo resolution: explicit flag > GITHUB_REPOSITORY env > fail.
    # No hardcoded fallback — we'd rather error out than silently emit
    # a manifest URL pointing at the wrong repo (the prior bug).
    org = args.gh_org
    repo = args.gh_repo
    if org is None or repo is None:
        inferred = _infer_repo()
        if inferred is None:
            print(
                "error: unable to determine release repo. Either run inside "
                "GitHub Actions (which sets GITHUB_REPOSITORY) or pass "
                "--gh-org and --gh-repo explicitly.",
                file=sys.stderr,
            )
            return 2
        if org is None:
            org = inferred[0]
        if repo is None:
            repo = inferred[1]

    # Resolve om-core source root: explicit flag > OM_CORE_SRC env > sibling
    # `om-core/` dir (workflow checks the private repo out next to this one).
    om_core_src = args.om_core_src
    if om_core_src is None:
        env_src = os.environ.get("OM_CORE_SRC", "").strip()
        if env_src:
            om_core_src = Path(env_src)
    if om_core_src is None:
        # GH Actions workflow does `path: om-core` under the workspace root,
        # which is two levels above this script (plugin_root/scripts/...).
        plugin_root = Path(__file__).resolve().parent.parent
        candidate = plugin_root.parent / "om-core"
        if candidate.is_dir():
            om_core_src = candidate
    if om_core_src is None:
        print(
            "error: om-core source tree not found. Pass --om-core-src or "
            "set OM_CORE_SRC, or run from a workspace where ../om-core "
            "is the checked-out om-core repo.",
            file=sys.stderr,
        )
        return 3
    spec_value = _read_spec_from_om_core(om_core_src)

    sha = sha256_of(args.binary)
    size = args.binary.stat().st_size
    # Asset name = the staged file's own name. The workflow names it
    # `om-core-bin-<target>.tar.xz` (onedir bundle archive) — no suffix
    # reconstruction, so any future packaging change flows through here
    # by the filename alone.
    asset_name = args.binary.name
    url = (
        f"https://github.com/{org}/{repo}/releases/download/"
        f"v{args.version}/{asset_name}"
    )

    manifest: dict[str, object] = {
        "version": args.version,
        "target": args.target,
        "om_core_bin_url": url,
        "om_core_bin_sha256": sha,
        "size_bytes": size,
        "spec": spec_value,
        "format": args.format,
    }
    # build_flags: omit when caller passes empty string.
    if args.build_flags:
        manifest["build_flags"] = args.build_flags
    if args.build_id:
        manifest["build_id"] = args.build_id

    out = args.output
    if out is None:
        plugin_root = Path(__file__).resolve().parent.parent
        out = plugin_root / "bin" / f"om-core-{args.target}" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"manifest written: {out}")
    print(f"  version: {args.version}")
    print(f"  target:  {args.target}")
    print(f"  sha256:  {sha}")
    print(f"  size:    {size} bytes ({size / (1 << 20):.1f} MB)")
    print(f"  url:     {url}")
    print(f"  spec:    {spec_value}  (read from {om_core_src})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
