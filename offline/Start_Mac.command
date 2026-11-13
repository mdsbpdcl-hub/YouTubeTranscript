#!/bin/bash
# YouTube Transcript Extractor - runs on this computer (macOS / Linux)
cd "$(dirname "$0")"
export YTE_OFFLINE=1
echo ""
echo "  YouTube Transcript Extractor - runs on this computer"
echo ""
PY=""
for c in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  Python 3.10 or newer is needed. Opening the download page..."
  open "https://www.python.org/downloads/macos/" 2>/dev/null || xdg-open "https://www.python.org/downloads/" 2>/dev/null
  read -r -p "  Install Python, then press Enter to close and run this file again." _
  exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "  First-time setup: preparing the app. This takes a few minutes..."
  "$PY" -m venv .venv || { echo "  Setup failed."; read -r _; exit 1; }
fi
if ! cmp -s requirements.txt .venv/installed-requirements.txt; then
  echo "  Installing / updating components..."
  .venv/bin/python -m pip install --upgrade pip --quiet --disable-pip-version-check
  .venv/bin/python -m pip install -r requirements.txt --quiet --disable-pip-version-check || { echo "  Setup failed. Check the internet connection."; read -r _; exit 1; }
  cp requirements.txt .venv/installed-requirements.txt
fi
mkdir -p "$HOME/.streamlit"
[ -f "$HOME/.streamlit/credentials.toml" ] || printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
echo ""
echo "  Starting... your browser will open at http://localhost:8501"
echo "  Keep this window open while you use the app. Close it to stop."
.venv/bin/python -m streamlit run streamlit_app.py --server.headless false --server.port 8501 --browser.gatherUsageStats false --logger.hideWelcomeMessage true
