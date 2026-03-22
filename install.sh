#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${EX_COD_TG_REPO_URL:-git+https://github.com/jekakiev/ex-cod-tg.git@main}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BIN_DIR="${EX_COD_TG_BIN_DIR:-$HOME/.local/bin}"
SKIP_SERVICE_INSTALL="${EX_COD_TG_SKIP_SERVICE_INSTALL:-0}"

detect_codex_bin() {
  if command -v codex >/dev/null 2>&1; then
    command -v codex
    return 0
  fi

  if command -v npm >/dev/null 2>&1; then
    local npm_prefix=""
    npm_prefix="$(npm config get prefix 2>/dev/null || true)"
    if [[ -n "$npm_prefix" && -x "$npm_prefix/bin/codex" ]]; then
      printf '%s\n' "$npm_prefix/bin/codex"
      return 0
    fi
  fi

  return 1
}

ensure_codex_cli() {
  local codex_bin=""
  codex_bin="$(detect_codex_bin || true)"
  if [[ -n "$codex_bin" ]]; then
    if ! command -v codex >/dev/null 2>&1; then
      export PATH="$(dirname "$codex_bin"):$PATH"
    fi
    echo "Codex CLI found: $codex_bin"
    return 0
  fi

  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required to install Codex CLI automatically." >&2
    echo "Install Node.js with npm and re-run this installer." >&2
    exit 1
  fi

  echo "Installing Codex CLI with npm..."
  npm install -g @openai/codex

  codex_bin="$(detect_codex_bin || true)"
  if [[ -z "$codex_bin" ]]; then
    local npm_prefix=""
    npm_prefix="$(npm config get prefix 2>/dev/null || true)"
    if [[ -n "$npm_prefix" && -d "$npm_prefix/bin" ]]; then
      export PATH="$npm_prefix/bin:$PATH"
      codex_bin="$(detect_codex_bin || true)"
    fi
  fi

  if [[ -z "$codex_bin" ]]; then
    echo "Codex CLI was installed, but 'codex' is still not available on PATH." >&2
    echo "Add your npm global bin directory to PATH and run this installer again." >&2
    exit 1
  fi

  echo "Codex CLI ready: $codex_bin"
}

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
"$VENV_DIR/bin/python" -m pip install --upgrade --force-reinstall --no-cache-dir "$REPO_URL"

ln -sf "$EX_COD_BIN" "$SHIM_PATH"

ensure_codex_cli

if [[ "$SKIP_SERVICE_INSTALL" == "1" ]]; then
  echo "Skipping service installation."
else
  echo "Starting service installation..."
  if [[ -r /dev/tty ]]; then
    "$EX_COD_BIN" service install </dev/tty
  else
    "$EX_COD_BIN" service install
  fi
fi

cat <<EOF

Installation complete.

Command:
  $SHIM_PATH

If 'ex-cod-tg' is not available in your shell yet, open a new terminal window
or run:
  export PATH="$BIN_DIR:\$PATH"
EOF
