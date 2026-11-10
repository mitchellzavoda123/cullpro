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


# ----------------------------------------------------------------- references


def reference_count() -> int:
    """Portfolio frames in references/. Re-read so new drops show without a restart."""
    E._refs = None
    return len(E.load_references()[0])


def open_references() -> None:
    E.REF_DIR.mkdir(exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(E.REF_DIR))          # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["open" if os.uname().sysname == "Darwin" else "xdg-open", str(E.REF_DIR)])
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------- cache


def _ck(path: str) -> str:
    # The grade version is part of the key: a new rubric or a new reference set
    # means old grades are ignored and the frame is graded again, once.
    st = os.stat(path)
    return f"{path}|{st.st_size}|{int(st.st_mtime)}|{E.grade_version()}"


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
    """Raw rubric scores -> what the UI shows. Uses the strict grade from cullpro_sd."""
    g = E.grade(s)
    reject = s.get("reject_reason", "none")
    return {
        "tier": g["tier"], "score": g["score"], "pct": g["pct"], "port": g["portfolio"],
        "dims": {k: s[k] for k in E.WEIGHTS},
        "sponsors": s.get("sponsors_visible", [])[:6],
        "shot": s.get("shot_type", ""),
        "reject": "" if reject == "none" else reject,
        "face": s.get("subject_face", ""),
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


# ----------------------------------------------------------------- finals
# Grading one frame at a time against fixed anchors is good at sorting a card into
# tiers and bad at telling the 3rd best frame from the 12th: they all come back as
# 4s. The finals put the strongest candidates side by side in one request and ask
# for an order. One call per card, a few cents.

FINALS_N, FINALS_EDGE = 16, 768
FINALS_CACHE = Path.home() / ".cullpro_finals.json"
_LABELS = [f"P{i:02d}" for i in range(1, FINALS_N + 1)]

FINALS_PROMPT = """You are the photo editor picking tonight's posts from one game. Above are the strongest frames from the card, each labeled. Order every label from the frame you would post first to the one you would post last.

Rank first on which frames would go in the photographer's portfolio, then on: the size of the moment (peak action or real emotion beats routine play), a sharp face on the main subject, whether the puck or ball and the context make the play readable, then clean framing. Color is fixed in editing, so do not rank on it, but a dark, muddy frame is worse than a bright, clean one. When two frames show the same moment, put the weaker one well down the list, since only one of them will be posted. If reference frames from the photographer's portfolio came first, they show the level they want; frames closest to that level go on top."""

FINALS_SCHEMA = {
    "type": "object",
    "properties": {"order": {"type": "array", "items": {"type": "string", "enum": _LABELS}}},
    "required": ["order"],
    "additionalProperties": False,
}


def _finals_key(paths) -> str:
    import hashlib
    keys = sorted(_ck(p) for p in paths)
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()


def _finals_load() -> dict:
    try:
        return json.loads(FINALS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _small_b64(path: str, edge: int) -> str:
    raw = base64.standard_b64decode(preview_b64(path))
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((edge, edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return base64.standard_b64encode(buf.getvalue()).decode()


def finals(paths, sport):
    """Order the finalists best first. Returns (ordered_paths, dollars, error_or_None).
    Cached per finalist set, so reopening a card costs nothing."""
    paths = list(paths)[:FINALS_N]
    if len(paths) < 3:
        return paths, 0.0, None
    key = _finals_key(paths)
    store = _finals_load()
    hit = store.get(key)
    if hit and set(hit) == set(paths):
        return hit, 0.0, None

    import anthropic
    client = anthropic.Anthropic(api_key=get_key(), max_retries=3)
    content = list(E.reference_blocks())
    for lab, p in zip(_LABELS, paths):
        content.append({"type": "text", "text": f"{lab}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": _small_b64(p, FINALS_EDGE)}})
    content.append({"type": "text", "text": FINALS_PROMPT})
    try:
        msg = client.messages.create(
            model=E.MODEL, max_tokens=2048,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": FINALS_SCHEMA}},
            system=[{"type": "text", "text": E.SPORT_NOTES[sport]}],
            messages=[{"role": "user", "content": content}],
        )
        usage = E.Usage()
        usage.add(msg.usage)
        text = next(b.text for b in msg.content if getattr(b, "type", "") == "text")
        order = json.loads(text)["order"]
    except Exception as e:  # noqa: BLE001
        return paths, 0.0, E.is_fatal(e) or f"finals skipped: {str(e)[:120]}"

    by_label = dict(zip(_LABELS, paths))
    ranked, seen = [], set()
    for lab in order:
        p = by_label.get(lab)
        if p and p not in seen:
            ranked.append(p)
            seen.add(p)
    ranked += [p for p in paths if p not in seen]      # anything it skipped keeps grade order
    store[key] = ranked
    try:
        FINALS_CACHE.write_text(json.dumps(store), encoding="utf-8")
    except OSError:
        pass
    return ranked, usage.dollars(False), None
