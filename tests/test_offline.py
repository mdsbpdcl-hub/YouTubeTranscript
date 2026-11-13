"""Tests for the "Run offline" package and its double-click starters."""

import io
import os
import zipfile

from modules.offline_package import TOP_FOLDER, build_offline_zip

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_offline_zip_has_starters_and_app_but_no_tests_or_secrets():
    zf = zipfile.ZipFile(io.BytesIO(build_offline_zip()))
    names = zf.namelist()
    for required in ["Start_Windows.bat", "Start_Mac.command", "HOW_TO_RUN_OFFLINE.txt", "streamlit_app.py",
                     "requirements.txt", ".streamlit/config.toml", "modules/batch_processor.py",
                     "assets/fonts/NotoSansDevanagari-Regular.ttf"]:
        assert f"{TOP_FOLDER}/{required}" in names, required
    assert not any("/tests/" in n or "secrets.toml" in n or "__pycache__" in n for n in names)
    mac = zf.getinfo(f"{TOP_FOLDER}/Start_Mac.command")
    assert (mac.external_attr >> 16) & 0o111  # executable


def test_windows_starter_is_crlf_ascii_and_sets_offline_flag():
    data = open(os.path.join(ROOT, "offline", "Start_Windows.bat"), "rb").read()
    data.decode("ascii")
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert b'set "YTE_OFFLINE=1"' in data
    assert b"streamlit run streamlit_app.py" in data
    assert b"credentials.toml" in data  # no first-run email prompt


def test_mac_starter_is_lf():
    data = open(os.path.join(ROOT, "offline", "Start_Mac.command"), "rb").read()
    assert data.startswith(b"#!/bin/bash") and b"\r\n" not in data
    assert b"export YTE_OFFLINE=1" in data
