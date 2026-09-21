"""Configuration storage: saved profiles, autostart, app preferences."""

from __future__ import annotations

import json
import os

APP_NAME = "audalis"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "audalis.log")
AUTOSTART_DIR = os.path.join(os.path.expanduser("~"), ".config", "autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "audalis-restore.desktop")


def _default() -> dict:
    return {"autostart": {"preset": ""}, "presets": {}, "preferences": {"advanced": False}}


def load() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    base = _default()
    base.update(data or {})
    return base


def save(data: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def write_autostart(preset: str) -> None:
    """Install/remove the login-time restore entry. Uses the audioctl CLI."""
    import shutil

    exe = shutil.which("audioctl")
    if preset:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        command = f"{exe or 'audioctl'} restore --preset {json.dumps(preset)}"
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Audalis restore\n"
            f"Exec={command}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-KDE-autostart-after=panel\n"
        )
        try:
            with open(AUTOSTART_FILE, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError:
            pass
    else:
        try:
            if os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
        except OSError:
            pass


def append_log(text: str) -> None:
    import time

    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n")
    except OSError:
        pass