#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${EX_COD_TG_REPO_URL:-git+https://github.com/jekakiev/ex-cod-tg.git}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BIN_DIR="${EX_COD_TG_BIN_DIR:-$HOME/.local/bin}"

detect_platform() {
  case "$(uname -s)" in
    Darwin)
      printf 'macos\n'
      ;;
    Linux)
      printf 'linux\n'
      ;;
    *)
      echo "Unsupported operating system. ex-cod-tg currently supports macOS and Linux." >&2
      exit 1
      ;;
  esac
}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.11+ is required." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11+ is required." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

PLATFORM="$(detect_platform)"
if [[ "$PLATFORM" == "macos" ]]; then
  INSTALL_ROOT="${EX_COD_TG_INSTALL_ROOT:-$HOME/Library/Application Support/ex-cod-tg/app}"
else
  INSTALL_ROOT="${EX_COD_TG_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/ex-cod-tg/app}"
fi

VENV_DIR="$INSTALL_ROOT/venv"
EX_COD_BIN="$VENV_DIR/bin/ex-cod-tg"
SHIM_PATH="$BIN_DIR/ex-cod-tg"

mkdir -p "$INSTALL_ROOT"
mkdir -p "$BIN_DIR"

echo "Creating virtual environment..."
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "Installing ex-cod-tg..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
"$VENV_DIR/bin/python" -m pip install --upgrade "$REPO_URL"

ln -sf "$EX_COD_BIN" "$SHIM_PATH"

echo "Starting service installation..."
if [[ -r /dev/tty ]]; then
  "$EX_COD_BIN" service install </dev/tty
else
  "$EX_COD_BIN" service install
fi

cat <<EOF

Installation complete.

Command:
  $SHIM_PATH

If 'ex-cod-tg' is not available in your shell yet, open a new terminal window
or run:
  export PATH="$BIN_DIR:\$PATH"
EOF
