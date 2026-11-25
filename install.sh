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

say "1/5 Finding Python 3.11 or newer"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    say "    installing Python 3.12 with Homebrew"
    brew install python@3.12
    PY="$(brew --prefix)/bin/python3.12"
  else
    if ! command -v uv >/dev/null 2>&1; then
      say "    installing uv (Astral's Python manager) to get Python 3.12"
      curl -LsSf https://astral.sh/uv/install.sh | sh
      PATH="$HOME/.local/bin:$PATH"
    fi
    uv python install 3.12
    PY="$(uv python find 3.12)"
  fi
fi
echo "    using $PY ($("$PY" --version))"

say "2/5 Creating $WHOME/venv and installing Wright"
mkdir -p "$WHOME"
"$PY" -m venv "$WHOME/venv"
"$WHOME/venv/bin/python" -m pip install -q --upgrade pip
if [ "$BROWSER" = 1 ]; then
  "$WHOME/venv/bin/python" -m pip install -q -e "$HERE[browser,test]"
else
  "$WHOME/venv/bin/python" -m pip install -q -e "$HERE[test]"
fi

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
    -e "do shell script \"$WHOME/venv/bin/wright run > $WHOME/logs/app.log 2>&1 &\"" >/dev/null
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
