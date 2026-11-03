"""
Starts CullPro and opens it in its own app window (no browser tabs, no black box).

The Desktop icon runs this with pythonw.exe. If CullPro is already open it just
brings up another window instead of starting a second copy.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

if sys.stdout is None or sys.stderr is None:          # pythonw has no console
    log = open(HERE / "cullpro.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log

PORT = int(os.environ.get("PHOTOCURATOR_PORT", "5014"))
URL = f"http://127.0.0.1:{PORT}/"


def alive() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/ping", timeout=1.5):
            return True
    except Exception:
        return False


def app_browser():
    env = os.environ
    cands = [
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which("chrome") or shutil.which("msedge") or shutil.which("google-chrome")


def open_window():
    exe = app_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={URL}", "--window-size=1500,950"], close_fds=True)
            return
        except Exception:
            pass
    webbrowser.open(URL)


def main():
    restarting = "--restart" in sys.argv
    if restarting:
        for _ in range(60):            # wait for the old copy to let go of the port
            if not alive():
                break
            time.sleep(0.25)
    elif alive():
        open_window()
        return

    os.environ["CULLPRO_NO_BROWSER"] = "1"
    os.environ["CULLPRO_APP"] = "1"

    if not restarting:                 # after an update the open window reloads itself
        def opener():
            for _ in range(120):
                if alive():
                    open_window()
                    return
                time.sleep(0.25)
        threading.Thread(target=opener, daemon=True).start()

    import runpy
    runpy.run_path(str(HERE / "photo_curator.py"), run_name="__main__")


if __name__ == "__main__":
    main()
