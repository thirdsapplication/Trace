#!/usr/bin/env bash
# Build an UNSIGNED Palza.app from an exact commit on GitHub, zipped for whoever
# holds the Developer ID certificate to sign with desktop/sign_mac.sh.
#
#   desktop/build_mac.sh [git-ref]        # branch, tag or sha; default: the repo's default branch
#
# Builds from a fresh clone in a temp dir, never from your working tree, so the zip
# is exactly what that commit says — no uncommitted edits, no iCloud-evicted files.
# Python 3.11 in a fresh venv, matching CI. Set PYTHON to pick the interpreter.
#
# Output: dist-mac/Palza-<version>-<sha>-unsigned.zip in the current directory.
set -euo pipefail

REF="${1:-}"
REPO="https://github.com/thirdsapplication/Trace"
PYTHON="${PYTHON:-python3.11}"
OUT_DIR="$(pwd)/dist-mac"

command -v "$PYTHON" >/dev/null || { echo "need $PYTHON (or set PYTHON=/path/to/python3.11)"; exit 1; }
command -v cargo >/dev/null || { echo "need Rust (cargo) + tauri-cli: cargo install tauri-cli"; exit 1; }

WORK="$(mktemp -d /tmp/palza-build.XXXXXX)"
echo "== building in $WORK"

if [ -n "$REF" ]; then
  GIT_TERMINAL_PROMPT=0 git clone -q "$REPO" "$WORK/src"
  git -C "$WORK/src" checkout -q "$REF"
else
  GIT_TERMINAL_PROMPT=0 git clone -q --depth 1 "$REPO" "$WORK/src"
fi
cd "$WORK/src"
SHA="$(git rev-parse --short HEAD)"
VERSION="$("$PYTHON" -c 'import json;print(json.load(open("src-tauri/tauri.conf.json"))["version"])')"
echo "== commit $SHA, version $VERSION"

echo "== python deps"
"$PYTHON" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q --upgrade pip
"$WORK/venv/bin/pip" install -q -r requirements.txt pyinstaller

echo "== trace-node (pyinstaller)"
"$WORK/venv/bin/pyinstaller" --noconfirm --log-level WARN desktop/trace-node.spec
mkdir -p src-tauri/binaries
cp -R dist/trace-node src-tauri/binaries/trace-node
# Finder metadata on any bundled file makes codesign emit an empty seal, which
# macOS then refuses to honour permission grants for. Strip it before bundling.
xattr -cr src-tauri

echo "== app (tauri)"
(cd src-tauri && cargo tauri build --bundles app)
APP="src-tauri/target/release/bundle/macos/Palza.app"

# Record exactly what this is, inside the bundle, before it is signed.
printf 'version=%s\ncommit=%s\nbuilt=%s\n' "$VERSION" "$(git rev-parse HEAD)" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$APP/Contents/Resources/BUILD_INFO"

mkdir -p "$OUT_DIR"
ZIP="$OUT_DIR/Palza-$VERSION-$SHA-unsigned.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
rm -rf "$WORK"
echo
echo "Built $ZIP"
echo "sha256: $(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
echo "Send the signer this hash separately (e.g. by text), not alongside the file."
echo "Send it to whoever signs; they run: desktop/sign_mac.sh \"$(basename "$ZIP")\""
