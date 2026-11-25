#!/bin/sh
# Wright installer — one command:   ./install.sh
# Creates ~/.wright/venv, installs Wright and its MCP tool servers, links `wright` into ~/.local/bin,
# adds a clickable Wright.app to ~/Applications (macOS), then runs a self-test.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
WHOME="${WRIGHT_HOME:-$HOME/.wright}"
BROWSER=1
for a in "$@"; do [ "$a" = "--no-browser" ] && BROWSER=0; done
say() { printf '\033[1m%s\033[0m\n' "$*"; }

say "1/5 Getting uv (Python and package manager) from PyPI, hash-checked"
# No piped remote scripts, no Homebrew, no admin rights (brief v3, section 12.3). uv comes from PyPI,
# pinned to one release, and pip refuses it unless its SHA-256 matches one of these.
UV_VERSION=0.13.10
SYSPY="$(command -v python3 || true)"
if [ -z "$SYSPY" ]; then
  echo "    python3 is missing. On a Mac, install Apple's Command Line Tools (run: xcode-select --install), then run this again."
  exit 1
fi
mkdir -p "$WHOME"
"$SYSPY" -m venv "$WHOME/bootstrap"
REQ="$WHOME/bootstrap/uv-requirements.txt"
cat > "$REQ" <<REQS
uv==$UV_VERSION \\
    --hash=sha256:19a76fd50a6a2bba38ee088d8336566856569be08196865f98132a08806581fc \\
    --hash=sha256:2a9ff188b442419c7aadebc445fcc1f287b25f99af50fa6d563b90cc0047b7cf \\
    --hash=sha256:7cc9c41b43d583e34c781fd3446405b06e9c5fa2db468ce2a534d353db6429e5 \\
    --hash=sha256:e89dd26ea831dde70639ba0a798fa04a4c7c0226fd2b241da86218bfa370e495
REQS
"$WHOME/bootstrap/bin/python" -m pip install -q --disable-pip-version-check --only-binary=:all: --require-hashes -r "$REQ"
UV="$WHOME/bootstrap/bin/uv"
echo "    uv $("$UV" --version | cut -d' ' -f2) ready"

say "2/5 Installing Python 3.12 and Wright into $WHOME/venv from the lockfile"
# uv.lock pins every package with hashes, resolved for Intel Macs, Apple-silicon Macs and Linux;
# --frozen installs exactly that (no re-resolving), and Python 3.12 is downloaded if not present.
EXTRAS="--extra test"
[ "$BROWSER" = 1 ] && EXTRAS="$EXTRAS --extra browser"
UV_PROJECT_ENVIRONMENT="$WHOME/venv" "$UV" sync --quiet --frozen --python 3.12 --project "$HERE" $EXTRAS

say "3/5 Browser tool"
if [ "$BROWSER" = 1 ]; then
  "$WHOME/venv/bin/python" -m playwright install chromium >/dev/null 2>&1 \
    && echo "    Chromium for the browser tool installed" \
    || echo "    browser tool skipped (playwright could not install Chromium; everything else works)"
else
  echo "    skipped (--no-browser)"
fi

say "4/5 Linking the wright command"
mkdir -p "$HOME/.local/bin"
ln -sf "$WHOME/venv/bin/wright" "$HOME/.local/bin/wright"
# Make `wright` available in every new Terminal window (check the shell's startup file, not this
# script's PATH, which may already include ~/.local/bin).
RC="$HOME/.zshrc"; [ "$(basename "${SHELL:-}")" = "bash" ] && RC="$HOME/.bash_profile"
if ! grep -qs '\.local/bin' "$RC"; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"   # added by Wright installer\n' >> "$RC"
  echo "    added ~/.local/bin to PATH in $RC"
fi
if [ "$(uname)" = "Darwin" ] && command -v osacompile >/dev/null 2>&1; then
  mkdir -p "$HOME/Applications"
  osacompile -o "$HOME/Applications/Wright.app" \
    -e "do shell script \"export PATH=/usr/local/bin:/opt/homebrew/bin:\$HOME/.local/bin:\$PATH; $WHOME/venv/bin/wright run > $WHOME/logs/app.log 2>&1 &\"" >/dev/null
  echo "    Wright.app created in ~/Applications (drag it to the Dock)"
fi
mkdir -p "$WHOME/logs"

say "5/5 Self-test"
if "$WHOME/venv/bin/wright" demo step1 >/dev/null 2>&1; then
  echo "    harness self-test passed"
else
  echo "    self-test FAILED — run: $WHOME/venv/bin/wright demo step1"; exit 1
fi
echo
say "Installed."
if [ "$(uname)" = "Darwin" ] && [ -d "$HOME/Applications/Wright.app" ] && [ "${WRIGHT_NO_LAUNCH:-0}" != "1" ]; then
  open "$HOME/Applications/Wright.app"
  echo "  The Wright window is opening. Next time, double-click Wright in the Applications"
  echo "  folder inside your home folder (or drag it to the Dock). Everything else happens in the window."
else
  echo "  Open Wright with: $WHOME/venv/bin/wright   (or just 'wright' in a new terminal window)"
fi
