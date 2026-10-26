#!/bin/sh
# macOS / Linux launcher: double-click (macOS) or run ./"BF6 Kill Tracker.command"
cd "$(dirname "$0")" || exit 1
if ! cmp -s requirements.txt .venv/installed.ok; then  # first run, or requirements changed
    echo "Setting up Python packages (first run or after an update)..."
    [ -x .venv/bin/python ] || python3 -m venv .venv || { echo "Python 3.9+ is required"; exit 1; }
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt || exit 1
    cp requirements.txt .venv/installed.ok
fi
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" exec .venv/bin/python -m bf6killtracker "$@"
