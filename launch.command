#!/bin/bash
# Tickets Touched Report — Mac/Linux launcher
# Double-click this file to start the app.
# Press Ctrl+C (or close this window) to stop the server.

cd "$(dirname "$0")"

# ── Python check ─────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found."
  echo "Install Python 3.9+ from https://www.python.org/downloads"
  read -rp "Press Enter to close…"
  exit 1
fi

# ── Virtual environment ───────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (first run only)…"
  python3 -m venv .venv
fi

source .venv/bin/activate

# ── Dependencies ──────────────────────────────────────────────────────────────
echo "Checking dependencies…"
pip install -r requirements.txt --quiet

# ── Open browser after a short delay ─────────────────────────────────────────
(sleep 2 && open http://localhost:8000) &

# ── Start server ──────────────────────────────────────────────────────────────
echo ""
echo "  Tickets Touched Report"
echo "  ──────────────────────────────"
echo "  URL : http://localhost:8000"
echo "  Stop: Ctrl+C  (or close this window)"
echo ""

python3 -m uvicorn app:app --port 8000
