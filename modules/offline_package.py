"""Build the "Run offline" ZIP: the app plus double-click starters.

The ZIP is assembled from the app's own files at run time, so it always
matches the deployed version. Tests, secrets and caches are never included.
"""

from __future__ import annotations

import io
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP_FOLDER = "YouTube-Transcript-Extractor"

# Files placed at the top of the folder so they are easy to find.
STARTERS = {
    "offline/Start_Windows.bat": "Start_Windows.bat",
    "offline/Start_Mac.command": "Start_Mac.command",
    "offline/HOW_TO_RUN_OFFLINE.txt": "HOW_TO_RUN_OFFLINE.txt",
}
APP_FILES = ["streamlit_app.py", "requirements.txt", "README.md", ".streamlit/config.toml"]
APP_FOLDERS = ["modules", "assets", "offline"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", "tests"}
SKIP_NAMES = {"secrets.toml"}


def _app_paths() -> list[str]:
    paths = [p for p in APP_FILES if os.path.exists(os.path.join(ROOT, p))]
    for folder in APP_FOLDERS:
        base = os.path.join(ROOT, folder)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_PARTS)
            for name in sorted(filenames):
                if name in SKIP_NAMES or name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(dirpath, name)
                paths.append(os.path.relpath(full, ROOT).replace(os.sep, "/"))
    return paths


def _write(zf: zipfile.ZipFile, source: str, target: str, executable: bool = False) -> None:
    info = zipfile.ZipInfo(f"{TOP_FOLDER}/{target}", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o755 if executable else 0o644) << 16
    with open(os.path.join(ROOT, source), "rb") as fh:
        zf.writestr(info, fh.read())


def build_offline_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for source, target in STARTERS.items():
            if os.path.exists(os.path.join(ROOT, source)):
                _write(zf, source, target, executable=target.endswith(".command"))
        for path in _app_paths():
            if path in STARTERS:
                continue
            _write(zf, path, path)
    return buffer.getvalue()
