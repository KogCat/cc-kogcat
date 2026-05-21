#!/usr/bin/env bash
# tools/pull-om-core.sh — fetch an om-core binary from a GitHub release
# and stage it under bin/om-core-<target>/om-core-bin so the wrapper can
# spawn it.
#
# Layout produced (PyInstaller --onedir bundle):
#   bin/om-core-<target>/om-core-bin[.exe]   the executable
#   bin/om-core-<target>/_internal/          bundled libs
#
# The CC plugin runtime injects $CLAUDE_PLUGIN_ROOT, the wrapper resolves
# bin/om-core-<target>/om-core-bin from there. Linux is deliberately not
# packaged.
#
# Release asset: `om-core-bin-<target>.tar.xz` for every target — the
# onedir bundle packed as an xz tar (members at the archive root). This
# script downloads it and extracts into bin/om-core-<target>/.
#
# Usage:
#   tools/pull-om-core.sh                  # pull host-target latest
#   tools/pull-om-core.sh --version 0.2.0  # pin to a specific om-core release
#   tools/pull-om-core.sh --target aarch64-apple-darwin
#
# Env:
#   OM_CORE_REPO    default: parsed from bin/om-core-<target>/manifest.json
#                   `om_core_bin_url` field. The manifest is the single
#                   source of truth — release_manifest.py writes whichever
#                   GITHUB_REPOSITORY ran release-om-core.yml into this URL
#                   at publish time, so pulling tracks publishing
#                   automatically. Override with --repo or OM_CORE_REPO env
#                   when staging a binary outside the standard pipeline.
#   OM_CORE_FORCE   default: 0; set to 1 to overwrite existing binary
#
set -euo pipefail

REPO="${OM_CORE_REPO:-}"
VERSION=""
TARGETS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2;;
        --target)  TARGETS+=("$2"); shift 2;;
        --repo)    REPO="$2"; shift 2;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed -E 's/^#[[:space:]]?//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

plugin_root="$(cd "$(dirname "$0")/.." && pwd)"
out_root="$plugin_root/bin"
mkdir -p "$out_root"

if [ ${#TARGETS[@]} -eq 0 ]; then
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64)  TARGETS=(aarch64-apple-darwin);;
        Darwin-x86_64) TARGETS=(x86_64-apple-darwin);;
        MINGW*|MSYS*|CYGWIN*) TARGETS=(x86_64-pc-windows-msvc);;
        *) echo "tools/pull-om-core.sh: unsupported host; pass --target" >&2; exit 2;;
    esac
fi

# REPO resolution: --repo > OM_CORE_REPO > parse from manifest.json > error.
# No hardcoded fallback — the manifest is the single source of truth so
# pulling automatically tracks wherever release-om-core.yml last published.
if [ -z "$REPO" ]; then
    first_target="${TARGETS[0]}"
    manifest="$plugin_root/bin/om-core-${first_target}/manifest.json"
    if [ -f "$manifest" ]; then
        REPO="$(sed -n 's|.*"om_core_bin_url"[[:space:]]*:[[:space:]]*"https://github.com/\([^/]*\)/\([^/]*\)/releases/.*|\1/\2|p' "$manifest" | head -1)"
    fi
fi

if [ -z "$REPO" ]; then
    echo "tools/pull-om-core.sh: cannot resolve release repo." >&2
    echo "  Pass --repo <owner/name>, set OM_CORE_REPO env, or stage a" >&2
    echo "  manifest.json under bin/om-core-<target>/ first." >&2
    exit 2
fi

if [ -z "$VERSION" ]; then
    if command -v gh >/dev/null 2>&1; then
        VERSION="$(gh release view --repo "$REPO" --json tagName -q .tagName 2>/dev/null || true)"
    fi
    if [ -z "$VERSION" ]; then
        VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
    fi
    [ -n "$VERSION" ] || { echo "could not resolve latest om-core version; pass --version" >&2; exit 1; }
fi

echo "om-core: pulling $VERSION from $REPO"

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/pull-om-core.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT

for target in "${TARGETS[@]}"; do
    case "$target" in
        *windows*) ext=".exe" ;;
        *) ext="" ;;
    esac
    asset="om-core-bin-${target}.tar.xz"
    target_dir="$out_root/om-core-${target}"
    bin_path="$target_dir/om-core-bin${ext}"

    if [ -x "$bin_path" ] && [ "${OM_CORE_FORCE:-0}" != "1" ]; then
        echo "  $target: already present at $bin_path (set OM_CORE_FORCE=1 to overwrite)"
        continue
    fi

    mkdir -p "$target_dir"
    url="https://github.com/$REPO/releases/download/$VERSION/$asset"
    echo "  $target: downloading $url"

    archive="$tmp_dir/$asset"
    if command -v gh >/dev/null 2>&1; then
        gh release download --repo "$REPO" "$VERSION" \
            --pattern "$asset" --output "$archive" --clobber 2>/dev/null \
            || curl -fsSL "$url" -o "$archive"
    else
        curl -fsSL "$url" -o "$archive"
    fi

    [ -s "$archive" ] || { echo "  $target: download produced empty file" >&2; exit 1; }

    # onedir bundle: the archive root holds om-core-bin + _internal/.
    # Drop any prior bundle, then extract alongside the git-tracked
    # manifest.json / VERSION already in target_dir.
    rm -rf "$target_dir/_internal" "$bin_path"
    tar -xJf "$archive" -C "$target_dir"

    [ -f "$bin_path" ] || { echo "  $target: archive missing $bin_path after extract" >&2; exit 1; }
    chmod +x "$bin_path"

    echo "  $target: installed → $bin_path"
done

echo "om-core: done. Set OM_CORE_BIN to override at runtime."
