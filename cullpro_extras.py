"""
CullPro additions on top of Photo Curator:

  * Free up space   move blurry shots, burst repeats and Claude rejects into a
                    holding folder, then empty it to get the drive space back
  * Instagram crops 4:5 copies of the best photos on Export
  * Updates         one click pulls the newest version from GitHub
  * App lifecycle   heartbeat so closing the window quits, and a Quit button
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from flask import jsonify, request

HERE = Path(__file__).resolve().parent
REPO = "mitchellzavoda123/cullpro"
TRASH = "_CullPro Trash"
KEEP_ON_UPDATE = {".venv", ".env", "cullpro.log", "__pycache__", ".git", "references"}
CATS = {
    "blurry": "Blurry shots",
    "repeats": "Burst repeats (sharpest one kept)",
    "rejects": "Claude rejects (missed focus, blink, nothing happening)",
}


def local_version() -> str:
    try:
        return (HERE / "VERSION").read_text().strip()
    except OSError:
        return "0"


def _vt(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n} B"


def _size(p) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def install(M):
    """M is the photo_curator module."""
    app, state = M.app, M.state
    beat = {"last": 0.0}

    # ------------------------------------------------------------ free up space

    def twins(path: str) -> list[str]:
        p = Path(path)
        out = []
        try:
            for sib in p.parent.glob(p.stem + ".*"):
                if str(sib) != path and sib.suffix.lower() in M.IMG_EXTS and sib.is_file():
                    out.append(str(sib))
        except OSError:
            pass
        return out

    def candidates() -> dict[str, list[str]]:
        folder = state.get("folder")
        if not folder:
            return {k: [] for k in CATS}
        f = str(folder)
        cull, dd = state["cull"], state["dedup"]
        blurry = []
        if cull.get("src_folder") == f:
            blurry = [p["path"] for p in cull.get("photos", []) if p.get("tier") == "blurry"]
        repeats = []
        if dd.get("src_folder") == f and dd.get("complete"):
            kept = set(map(str, dd.get("kept_paths", [])))
            repeats = [p for p in dd.get("input_paths", []) if p not in kept]
        rejects = []
        survivors = set(map(str, dd.get("kept_paths", []))) if dd.get("src_folder") == f else None
        for p, a in state.get("ai", {}).items():
            if a and a.get("tier") == "REJECT" and str(Path(p).parent) == str(Path(f)):
                if survivors is None or p in survivors:
                    rejects.append(p)
        out = {"blurry": blurry, "repeats": repeats, "rejects": rejects}
        # bring along the RAW/JPG twin of each frame, unless the twin is a keeper
        keepers = set()
        if dd.get("src_folder") == f:
            keepers = set(map(str, dd.get("kept_paths", []))) - set(rejects)
        seen = set()
        for k in out:
            full = []
            for p in out[k]:
                for q in [p, *twins(p)]:
                    if q in seen or (q != p and q in keepers):
                        continue
                    if os.path.exists(q):
                        seen.add(q)
                        full.append(q)
            out[k] = full
        return out

    def trash_dir() -> Path | None:
        f = state.get("folder")
        return Path(f) / TRASH if f else None

    def trash_info():
        t = trash_dir()
        n = b = 0
        if t and t.is_dir():
            for p in t.rglob("*"):
                if p.is_file() and p.name != "manifest.json":
                    n += 1
                    b += _size(p)
        return {"n": n, "bytes": b, "size": human(b), "path": str(t) if t else ""}

    @app.route("/api/cleanup", methods=["GET"])
    def cleanup_info():
        c = candidates()
        cats = {}
        for k, label in CATS.items():
            b = sum(_size(p) for p in c[k])
            cats[k] = {"label": label, "n": len(c[k]), "bytes": b, "size": human(b)}
        ran = {
            "cull": state["cull"].get("src_folder") == str(state.get("folder")),
            "dedup": state["dedup"].get("src_folder") == str(state.get("folder")) and state["dedup"].get("complete"),
            "ai": bool(state.get("ai")),
        }
        return jsonify({"cats": cats, "trash": trash_info(), "ran": ran, "folder": state.get("folder")})

    @app.route("/api/cleanup", methods=["POST"])
    def cleanup_run():
        want = [k for k in (request.get_json() or {}).get("cats", []) if k in CATS]
        t = trash_dir()
        if not t or not want:
            return jsonify({"error": "Nothing picked."}), 400
        c = candidates()
        manifest_path = t / "manifest.json"
        t.mkdir(parents=True, exist_ok=True)
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        moved, b, failed = [], 0, 0
        for k in want:
            (t / k).mkdir(exist_ok=True)
            for src in c[k]:
                try:
                    dst = t / k / Path(src).name
                    i = 1
                    while dst.exists():
                        dst = t / k / f"{Path(src).stem}_{i}{Path(src).suffix}"
                        i += 1
                    sz = _size(src)
                    shutil.move(src, dst)
                    manifest[str(dst)] = src
                    moved.append(src)
                    b += sz
                except OSError:
                    failed += 1
        manifest_path.write_text(json.dumps(manifest))
        # Only hide them from the ranking. The step results stay intact so
        # "Put them all back" restores everything exactly as it was.
        state["excluded"] |= set(moved)
        return jsonify({"moved": len(moved), "size": human(b), "failed": failed, "trash": trash_info()})

    @app.route("/api/cleanup/empty", methods=["POST"])
    def cleanup_empty():
        t = trash_dir()
        info = trash_info()
        if t and t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        return jsonify({"freed": info["size"], "n": info["n"]})

    @app.route("/api/cleanup/restore", methods=["POST"])
    def cleanup_restore():
        t = trash_dir()
        mp = t / "manifest.json" if t else None
        n = 0
        if mp and mp.exists():
            manifest = json.loads(mp.read_text())
            for dst, src in list(manifest.items()):
                try:
                    if Path(dst).exists() and not Path(src).exists():
                        shutil.move(dst, src)
                        state["excluded"].discard(src)
                        n += 1
                        manifest.pop(dst)
                except OSError:
                    pass
            mp.write_text(json.dumps(manifest))
            if not manifest:
                shutil.rmtree(t, ignore_errors=True)
        return jsonify({"restored": n, "trash": trash_info()})

    # ------------------------------------------------------------ instagram crops

    def instagram_crops(top: list[dict], dest: Path) -> int:
        picks = [t for t in top if t.get("ai") and t["ai"].get("tier") == "HERO"] or top[:10]
        out = dest / "Instagram 4x5"
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for item in picks[:30]:
            try:
                img = M.ImageOps.exif_transpose(M.open_image_pil(item["path"])).convert("RGB")
                w, h = img.size
                tw = min(w, int(h * 4 / 5))
                th = int(tw * 5 / 4)
                left, top_ = (w - tw) // 2, (h - th) // 2
                img = img.crop((left, top_, left + tw, top_ + th)).resize((1080, 1350), M.Image.LANCZOS)
                img.save(out / f"{item['rank']:03d}_{Path(item['path']).stem}_4x5.jpg", "JPEG", quality=92)
                n += 1
            except Exception as e:  # noqa: BLE001
                M.logger.warning(f"ig crop fail {item['path']}: {e}")
        return n


    # ------------------------------------------------------------ updates

    remote = {"v": None, "notes": "", "at": 0.0}

    def check_remote(force=False):
        if not force and time.time() - remote["at"] < 600:
            return
        remote["at"] = time.time()
        try:
            base = f"https://raw.githubusercontent.com/{REPO}/main/"
            with urllib.request.urlopen(base + "VERSION", timeout=5) as r:
                remote["v"] = r.read().decode().strip()
            try:
                with urllib.request.urlopen(base + "WHATS_NEW.txt", timeout=5) as r:
                    remote["notes"] = r.read().decode()[:1500]
            except Exception:  # noqa: BLE001
                remote["notes"] = ""
        except Exception:  # noqa: BLE001
            remote["v"] = None

    @app.route("/api/version")
    def version():
        check_remote(force=request.args.get("force") == "1")
        lv, rv = local_version(), remote["v"]
        return jsonify({"local": lv, "remote": rv, "notes": remote["notes"],
                        "available": bool(rv and _vt(rv) > _vt(lv))})

    def _req_hash() -> str:
        h = hashlib.sha1()
        for n in ("requirements.txt", "requirements-optional.txt"):
            p = HERE / n
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()

    @app.route("/api/update", methods=["POST"])
    def update():
        try:
            url = f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
            before = _req_hash()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                root = z.namelist()[0].split("/")[0] + "/"
                for name in z.namelist():
                    rel = name[len(root):]
                    if not rel or rel.endswith("/"):
                        continue
                    if rel.split("/")[0] in KEEP_ON_UPDATE:
                        continue
                    dst = HERE / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_suffix(dst.suffix + ".new")
                    tmp.write_bytes(z.read(name))
                    os.replace(tmp, dst)
            if _req_hash() != before:
                flags = 0x08000000 if sys.platform.startswith("win") else 0
                py = sys.executable.replace("pythonw.exe", "python.exe")
                for req in ("requirements.txt", "requirements-optional.txt"):
                    if (HERE / req).exists():
                        subprocess.run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                        "-r", str(HERE / req)], cwd=HERE, creationflags=flags)
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": f"Update failed: {e}"}), 500

        def restart():
            time.sleep(0.6)
            flags = 0x00000008 | 0x00000200 if sys.platform.startswith("win") else 0  # DETACHED | NEW_GROUP
            subprocess.Popen([sys.executable, str(HERE / "launcher.py"), "--restart"], cwd=HERE,
                             creationflags=flags, close_fds=True)
            os._exit(0)

        threading.Thread(target=restart, daemon=True).start()
        return jsonify({"ok": True, "version": local_version()})

    # ------------------------------------------------------------ lifecycle

    @app.route("/api/ping", methods=["GET", "POST"])
    def ping():
        beat["last"] = time.time()
        return jsonify({"ok": True, "version": local_version()})

    @app.route("/api/quit", methods=["POST"])
    def quit_app():
        threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
        return jsonify({"ok": True})

    def busy() -> bool:
        return any(state[s].get("running") for s in ("cull", "dedup", "rank"))

    def watchdog():
        # Closing the CullPro window stops the pings; quit a few minutes later
        # so nothing keeps running in the background. Never mid-job.
        while True:
            time.sleep(20)
            if beat["last"] and time.time() - beat["last"] > 240 and not busy():
                os._exit(0)

    if os.environ.get("CULLPRO_APP"):
        threading.Thread(target=watchdog, daemon=True).start()
    return instagram_crops
