"""Start Night Reader when Windows starts, so photos from the phone go straight in after a
reboot with nobody at the computer.

The entry is a value in the current user's Run key: no admin rights, no console window
(it runs ``pythonw``), and it shows in Task Manager → Startup apps, where it can also be
switched off. It starts the app with ``--background``: no browser tab, output to a log.

Every registry call goes through ``reg``, so the tests run on any system.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "NightReader"
ROOT = Path(__file__).resolve().parent.parent


def supported() -> bool:
    return sys.platform == "win32"


def command(root: Path = ROOT) -> str:
    """What Windows runs: this copy's own Python, without a console, in the background."""
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    return f'"{pythonw}" "{root / "launch.py"}" --background'


def _reg(reg):
    if reg is not None:
        return reg
    import winreg  # Windows only; supported() is checked before anything gets here

    return winreg


def state(reg=None, *, root: Path = ROOT) -> dict:
    """Whether it starts with Windows, and whether the entry still points at this copy —
    a folder moved or copied elsewhere leaves an entry that starts nothing."""
    if reg is None and not supported():
        return {"supported": False, "on": False, "current": False}
    reg = _reg(reg)
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
            value, _kind = reg.QueryValueEx(key, NAME)
    except OSError:
        return {"supported": True, "on": False, "current": False}
    return {"supported": True, "on": True, "current": value == command(root)}


def set_on(on: bool, reg=None, *, root: Path = ROOT) -> dict:
    reg = _reg(reg)
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        if on:
            reg.SetValueEx(key, NAME, 0, reg.REG_SZ, command(root))
        else:
            try:
                reg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass
    return state(reg, root=root)
