#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '\n%s\n' "$*" >&2
  exit 1
}

cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || fail "Cannot open the ClipForge folder. Extract the entire downloaded ZIP first."
[[ -f clipforge/__main__.py ]] || fail "ClipForge source files are missing. Extract the entire downloaded ZIP first."
printf '\nClipForge - local video studio\n\n'

venv_python="$PWD/.venv/bin/python"
if [[ ! -x "$venv_python" ]]; then
  candidate=()
  for command_name in python3 python; do
    if command -v "$command_name" >/dev/null 2>&1 && "$command_name" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      candidate=("$command_name")
      break
    fi
  done
  if [[ ${#candidate[@]} -eq 0 ]] && command -v py >/dev/null 2>&1 && py -3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    candidate=(py -3)
  fi
  [[ ${#candidate[@]} -gt 0 ]] || fail "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ or your system package manager, then run this launcher again."
  printf "Creating ClipForge's private Python environment...\n"
  "${candidate[@]}" -m venv .venv || fail "Could not create .venv. Check that this folder is writable and Python includes venv. On Debian/Ubuntu, install the matching python3-venv package."
fi

"$venv_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || fail "The existing .venv does not contain a working Python 3.11 or newer. Rename .venv, install a supported Python, and run this launcher again."
if ! "$venv_python" -c "import pydantic, requests, filelock, tzdata, yt_dlp, yt_dlp_ejs; assert int(pydantic.__version__.split('.')[0]) == 2" >/dev/null 2>&1; then
  printf "Installing ClipForge and its local link importer. Internet is needed for setup.\n"
  "$venv_python" -m pip install --disable-pip-version-check -e '.[links]' || fail "Setup failed. Check your internet connection and the error above, then run this launcher again. FFmpeg, Ollama, and AI models are installed separately."
fi

printf 'Opening your browser. Keep this terminal open while using ClipForge.\nPress Ctrl+C here to stop the local server.\n\n'
exec "$venv_python" -m clipforge serve "$@"
