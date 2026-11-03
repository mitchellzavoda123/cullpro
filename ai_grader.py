"""
Claude grading for CullPro (added on top of Photo Curator).

Each photo is shrunk to a 1024px preview and scored by Claude on the sports
rubric in cullpro_sd.py: moment, technical, composition, postability, story,
sponsor. Results are cached on disk, so a photo is never paid for twice.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import io
import json
import os
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

import cullpro_sd as E
from raw_loader import open_image_pil, is_raw, is_heif

HERE = Path(__file__).resolve().parent
CACHE = Path.home() / ".cullpro_ai_cache.jsonl"
_lock = threading.Lock()
_cache: dict | None = None


# ----------------------------------------------------------------- key


def get_key() -> str | None:
    return E.find_api_key()


def check_and_save_key(key: str) -> str | None:
    """Returns an error message, or None when the key works and is saved."""
    import anthropic
    key = (key or "").strip().strip('"').strip("'")
    if not key.startswith("sk-ant-") or "..." in key or len(key) < 40:
        return "That isn't a whole key. Click Copy next to the key on platform.claude.com and paste again."
    try:
        anthropic.Anthropic(api_key=key, max_retries=1).messages.count_tokens(
            model=E.MODEL, messages=[{"role": "user", "content": "hi"}])
    except anthropic.AuthenticationError:
        return "That key didn't work. It may be deleted or expired. Make a new one."
    except anthropic.APIConnectionError:
        return "Can't reach Claude. Check your internet."
    except Exception as e:  # noqa: BLE001
        return f"Couldn't check the key: {e}"
    (HERE / ".env").write_text(f"ANTHROPIC_API_KEY={key}\n")
    os.environ["ANTHROPIC_API_KEY"] = key
    return None


# ----------------------------------------------------------------- cache


def _ck(path: str) -> str:
    st = os.stat(path)
    return f"{path}|{st.st_size}|{int(st.st_mtime)}"


def _load():
    global _cache
    if _cache is None:
        _cache = {}
        if CACHE.exists():
            for line in CACHE.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    r = json.loads(line)
                    _cache[r["k"]] = r["s"]
                except Exception:
                    pass
    return _cache


def cached(path: str):
    try:
        return _load().get(_ck(path))
    except OSError:
        return None


def _store(path: str, s: dict):
    with _lock:
        k = _ck(path)
        _load()[k] = s
        with CACHE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"k": k, "s": s}) + "\n")


# ----------------------------------------------------------------- scoring


def summarize(s: dict) -> dict:
    """Raw rubric scores -> what the UI shows."""
    w = round(sum(s[k] * v for k, v in E.WEIGHTS.items()), 2)
    tier = "REJECT"
    for name, cut in E.TIERS:
        if w >= cut:
            tier = name
            break
    reject = s.get("reject_reason", "none")
    if reject != "none":
        tier = "REJECT"
    return {
        "tier": tier, "score": w, "pct": round((w - 1) / 4 * 100, 1),
        "dims": {k: s[k] for k in E.WEIGHTS},
        "sponsors": s.get("sponsors_visible", [])[:6],
        "shot": s.get("shot_type", ""),
        "reject": "" if reject == "none" else reject,
    }


def preview_b64(path: str) -> str:
    if is_raw(path) or is_heif(path):
        img = open_image_pil(path)
    else:
        img = Image.open(path)
        img.draft("RGB", (E.PROXY_EDGE, E.PROXY_EDGE))   # fast reduced JPEG decode
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((E.PROXY_EDGE, E.PROXY_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode()


def run(paths, sport, on_result, on_progress, cancelled, workers=8):
    """Grade paths. Returns (graded_count, dollars_spent, error_or_None)."""
    import anthropic
    client = anthropic.Anthropic(api_key=get_key(), max_retries=4)
    usage = E.Usage()
    total, done, graded, fails = len(paths), 0, 0, 0
    stop = threading.Event()

    def one(p):
        if stop.is_set() or cancelled():
            return p, None
        msg = client.messages.create(
            **E.request_params(sport, preview_b64(p), E.MODEL),
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        usage.add(msg.usage)
        return p, E.parse_scores(msg)

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, p): p for p in paths}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            try:
                _, raw = fut.result()
                if raw is not None:
                    _store(p, raw)
                    on_result(p, summarize(raw))
                    graded += 1
                    fails = 0
            except Exception as e:  # noqa: BLE001
                fails += 1
                why = E.is_fatal(e)
                if why or fails >= 6:
                    stop.set()
                    for f in futs:
                        f.cancel()
                    return graded, usage.dollars(False), why or f"Claude kept failing: {str(e)[:160]}"
            done += 1
            on_progress(done, total, usage.dollars(False))
            if cancelled():
                stop.set()
    return graded, usage.dollars(False), None
