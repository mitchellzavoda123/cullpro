#!/usr/bin/env python3
"""
CullPro SD: cull a gameday card straight off the SD card.

    python cullpro_sd.py                     # find the card, estimate, confirm, cull
    python cullpro_sd.py --dry-run           # local pass only, no API spend
    python cullpro_sd.py --batch             # half price, results in ~minutes to an hour
    python cullpro_sd.py --card /Volumes/Untitled --sport baseball

The card is opened read only. Nothing is ever written to it.
Everything (proxies, scores, PICKS, report) lands in a local session folder,
so a rerun on the same card picks up where it stopped and never pays twice.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import csv
import html
import io
import json
import os
import platform
import shutil
import string
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Missing packages. Run:  pip install -r requirements.txt")

Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------- config

MODEL = "claude-opus-5"
PRICE_IN, PRICE_OUT = 5.00, 25.00          # $ per 1M tokens, Opus 5 standard rates
PROXY_EDGE = 1024                          # long edge sent to the API
THUMB_EDGE = 420                           # long edge in the HTML contact sheet
JPG_EXT = {".jpg", ".jpeg"}
RAW_EXT = {".arw", ".cr2", ".cr3", ".nef", ".raf", ".rw2", ".orf", ".dng"}

WEIGHTS = {
    "moment": 0.24, "technical": 0.22, "composition": 0.20,
    "postability": 0.14, "story": 0.12, "sponsor": 0.08,
}
TIERS = [("HERO", 4.2), ("KEEPER", 3.5), ("MAYBE", 2.8)]   # below MAYBE is REJECT
XMP_RATING = {"HERO": 5, "KEEPER": 4, "MAYBE": 3}

# --------------------------------------------------------------------------- rubric
# Byte stable for the whole run: it is the cached prefix. Edit between cards, never mid run.

RUBRIC = """You are the photo editor for a sports media team. You will see one frame at a time from a gameday memory card. Score it against this rubric and return only the JSON the schema asks for. Judge every frame against the written anchors below, never against other frames you may have seen, and never against how good the rest of the card might be.

The images are 1024 pixel proxies of 24 megapixel originals. Do not penalize mild softness that comes from the downscale itself. Do penalize softness on the subject's face, eyes, jersey number or the puck/ball when it is plainly missed focus or motion blur.

DIMENSIONS (each scored 1 to 5)

moment: how much happens in the frame.
5 = peak action or peak emotion: the shot leaving the stick or bat, a collision at its height, a goal or run celebration with open mouths and raised arms, a save at full extension.
4 = clear action with intent: a player driving to the net, a pitcher at release, a battle along the boards, a slide into a base.
3 = routine play: skating with the puck, a fielder getting set, a faceoff before the drop, a batter in the box.
2 = between plays: players gliding, standing, waiting, line changes, walking off the field.
1 = nothing happening: empty ice or field, backs of heads, a referee alone, a frame fired by accident.

technical: focus, exposure and noise on the main subject.
5 = eyes or face tack sharp, motion frozen, exposure clean on skin and jersey, no clipped whites on the jersey.
4 = subject sharp with small flaws: slight noise, a touch of blur on extremities only, exposure a third of a stop off and easily fixed.
3 = usable after editing: focus slightly behind or in front of the face but the number and body are sharp, or exposure needs a clear correction.
2 = focus missed the subject (sharp background, soft player), visible motion blur on the face, or blown highlights across the subject.
1 = unusable: badly out of focus, heavy shake, nearly black or nearly white.

composition: framing of the subject.
5 = subject placed with intent, room in the direction of travel, clean background, nothing important cut off, the frame would need no crop.
4 = strong framing that needs a small crop or straightening.
3 = subject is the clear focus but the frame is loose, centered by default, or the background is busy (glass glare, scoreboard clutter, crowds of other players).
2 = the subject is small in the frame, cut at the joints (hands, feet, stick blade, head), or blocked by another player or an official.
1 = no clear subject, or the subject is mostly out of frame.

postability: how well it works as a social media post on its own, judged for a 4:5 Instagram crop and a 9:16 story crop.
5 = scroll stopping, readable at phone size, works as the lead image of a carousel.
4 = strong carousel image, works at phone size.
3 = fills out a gallery or a carousel but would not lead it.
2 = only useful as an archive or record shot.
1 = no social use.

story: whether a viewer understands what happened from the frame alone.
5 = the frame tells the game: scoring play with the puck or ball visible, a team pile on, a dejected opponent in the background of a celebration, a coach's reaction at a key moment.
4 = clear context: the opponent, the net or base, the play are all readable.
3 = a single player doing a thing, context is thin.
2 = context is missing or confusing.
1 = no story.

sponsor: value of visible partner branding for a sponsorship report.
5 = a sponsor logo (dasher board, outfield wall, jersey patch, scoreboard, ice logo, giveaway item) is sharp, readable and sits naturally in a strong action frame.
4 = a logo is readable and in a usable frame.
3 = a logo is partly visible or soft but identifiable.
2 = branding is present but not readable.
1 = no partner branding visible.
In sponsors_visible, list the brand names you can actually read, exactly as printed, at most six. Return an empty list when none are readable. Never guess a brand from a color or a shape.

shot_type: pick the single best label.
action = live play. celebration = scoring or winning reaction. bench = players or staff on the bench or in the dugout. crowd = fans as the subject. portrait = a posed or near posed single player. venue = the rink, field, scoreboard or arena as the subject. ceremony = anthems, puck drops, first pitches, presentations, giveaways. other = anything else.

reject_reason: the one problem that makes this frame unusable, or none. Use none whenever technical is 3 or higher and the subject is in frame. Choose soft_focus for missed focus, motion_blur for shake or subject blur, subject_occluded when another body or the glass blocks the subject, eyes_closed for a blink on a featured face, back_to_camera when the only subject faces away, blown_highlights or underexposed for exposure failures that editing cannot rescue, cropped_limb when the frame cuts the subject badly at the joints, no_subject for empty or accidental frames.

Score honestly. Most frames on a gameday card are 2s and 3s. A 5 in moment or technical should be rare, and a card that came back with mostly 4s would be useless for culling."""

SPORT_NOTES = {
    "hockey": """SPORT: ice hockey, often under arena LED lighting through glass. Ice reads bright white; that is correct exposure, not blown highlights, as long as jerseys and faces hold detail. Glass reflections and smudges count against technical only when they cross the subject. Peak moments: shot release, save at full extension, goal celebration at the glass, big hits on the boards, a breakaway with the goalie in frame. A faceoff is a 3 in moment unless the puck is in the air at the drop. Dasher boards and center ice logos are the main sponsor surfaces.""",
    "baseball": """SPORT: baseball, day or night under stadium lights. Peak moments: ball on the bat, pitcher at release with the ball visible, a play at the plate, a diving catch, a home run trot with teammates waiting at home, a mascot or promotion with crowd reaction. A batter simply standing in the box is a 3 in moment. Outfield wall signs, the scoreboard, dugout signage and giveaway items are the main sponsor surfaces.""",
    "general": """SPORT: mixed or unspecified. Apply the rubric as written and judge peak moments by the sport you can see in the frame.""",
}

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "moment": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "technical": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "composition": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "postability": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "story": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "sponsor": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "shot_type": {"type": "string", "enum": [
            "action", "celebration", "bench", "crowd", "portrait", "venue", "ceremony", "other"]},
        "sponsors_visible": {"type": "array", "items": {"type": "string"}},
        # One type per property and no null inside an enum: "none" is the sentinel.
        "reject_reason": {"type": "string", "enum": [
            "none", "soft_focus", "motion_blur", "subject_occluded", "eyes_closed",
            "back_to_camera", "blown_highlights", "underexposed", "cropped_limb", "no_subject"]},
    },
    "required": ["moment", "technical", "composition", "postability", "story", "sponsor",
                 "shot_type", "sponsors_visible", "reject_reason"],
    "additionalProperties": False,
}

USER_TEXT = "Score this frame against the rubric."

# --------------------------------------------------------------------------- data


@dataclass
class Frame:
    stem: str
    jpg: str | None = None
    raw: str | None = None
    size: int = 0
    taken: float = 0.0
    taken_str: str = ""
    width: int = 0
    height: int = 0
    sharp: float = 0.0
    clip_hi: float = 0.0
    clip_lo: float = 0.0
    dhash: int = 0
    burst: int = 0
    burst_rep: bool = True
    status: str = "pending"      # pending | local_reject | burst_dup | scored | unscored | error
    local_reason: str = ""
    scores: dict | None = None
    weighted: float | None = None
    tier: str = ""
    error: str = ""

    @property
    def key(self) -> str:        # stable across reruns of the same card
        return f"{self.stem}:{self.size}"

    @property
    def source(self) -> str:
        return self.jpg or self.raw


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- API key


def find_api_key() -> str | None:
    """Same lookup order as the generate-image skill: env var, then a .env walking up."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    here = Path(__file__).resolve().parent
    for d in [Path.cwd(), *Path.cwd().parents, here, *here.parents]:
        env = d / ".env"
        if env.is_file():
            for line in env.read_text(errors="ignore").splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return None


# --------------------------------------------------------------------------- card


def candidate_volumes() -> list[Path]:
    sysname = platform.system()
    vols: list[Path] = []
    if sysname == "Darwin":
        vols = [p for p in Path("/Volumes").glob("*") if p.is_dir()]
    elif sysname == "Windows":
        vols = [Path(f"{c}:/") for c in string.ascii_uppercase[3:] if Path(f"{c}:/").exists()]
    else:
        user = os.environ.get("USER", "")
        for base in (Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
            if base.is_dir():
                vols += [p for p in base.iterdir() if p.is_dir()]
    return vols


def find_card(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_dir():
            sys.exit(f"Card path not found: {p}")
        return p
    cards = [v for v in candidate_volumes() if (v / "DCIM").is_dir()]
    log("Which photos should I cull?")
    for i, c in enumerate(cards, 1):
        try:
            n = sum(1 for p in (c / "DCIM").rglob("*") if p.suffix.lower() in JPG_EXT | RAW_EXT)
        except OSError:
            n = "?"
        log(f"  {i}. {c}   ({n} photos)")
    log("  Or drag any folder into this window and press Enter.")
    while True:
        pick = input("Type a number or drop a folder: ").strip().strip('"').strip("'").strip("&").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(cards):
            return cards[int(pick) - 1]
        p = Path(pick).expanduser()
        if pick and p.is_dir():
            return p
        log("Didn't get that. Type one of the numbers above, or drag a folder in.")


def scan_card(card: Path) -> list[Frame]:
    root = card / "DCIM" if (card / "DCIM").is_dir() else card
    frames: dict[str, Frame] = {}
    sizes: dict[str, int] = {}
    skipped: list[str] = []

    def walk(d: str) -> None:
        # One directory listing per folder, size taken from the same listing, so a flaky
        # reader or a damaged entry costs that one file instead of the whole run.
        try:
            entries = list(os.scandir(d))
        except OSError as e:
            skipped.append(f"{d} ({e.strerror or e})")
            return
        for e in entries:
            try:
                if e.is_dir():
                    walk(e.path)
                    continue
                if e.name.startswith("._") or not e.is_file():
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                if ext not in JPG_EXT and ext not in RAW_EXT:
                    continue
                size = e.stat().st_size
            except OSError:
                skipped.append(e.path)
                continue
            p = Path(e.path)
            # Sony restarts numbering per folder, so the folder is part of the identity.
            stem = f"{p.parent.name}/{p.stem}"
            f = frames.setdefault(stem, Frame(stem=stem))
            if ext in JPG_EXT:
                f.jpg = e.path
            else:
                f.raw = e.path
            sizes[e.path] = size

    walk(str(root))
    for f in frames.values():
        f.size = sizes[f.source]
    if skipped:
        log(f"Skipped {len(skipped)} file(s) the card wouldn't let me read:")
        for s in skipped[:10]:
            log(f"  {s}")
        if len(skipped) > 10:
            log(f"  ...and {len(skipped) - 10} more")
    return list(frames.values())


# --------------------------------------------------------------------------- decode


def exif_time(img: Image.Image) -> tuple[float, str]:
    try:
        ex = img.getexif()
        sub = ex.get_ifd(0x8769)
        raw = sub.get(36867) or ex.get(306)
        if not raw:
            return 0.0, ""
        ss = str(sub.get(37521) or "0").strip() or "0"
        dt = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
        return dt.timestamp() + float("0." + "".join(ch for ch in ss if ch.isdigit()) or 0), str(raw)
    except Exception:
        return 0.0, ""


def open_raw_preview(path: str) -> Image.Image:
    """Pull the biggest embedded JPEG out of a RAW without demosaicing it."""
    with open(path, "rb") as fh:
        data = fh.read(12 * 1024 * 1024)
    best, best_px = None, 0
    i = data.find(b"\xff\xd8\xff")
    while i != -1:
        try:
            im = Image.open(io.BytesIO(data[i:]))
            px = im.size[0] * im.size[1]
            if px > best_px and im.format == "JPEG":
                best, best_px = i, px
        except Exception:
            pass
        i = data.find(b"\xff\xd8\xff", i + 4)
    if best is None:
        try:
            import rawpy  # optional
            with rawpy.imread(path) as r:
                return Image.fromarray(r.postprocess(half_size=True))
        except Exception:
            raise ValueError("no embedded preview")
    return Image.open(io.BytesIO(data[best:]))


def dhash(gray: Image.Image) -> int:
    a = np.asarray(gray.resize((9, 8), Image.BILINEAR), dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def laplacian_var(gray: np.ndarray) -> float:
    g = gray.astype(np.float32)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def make_proxy(f: Frame, proxy_dir: Path, thumb_dir: Path) -> Frame:
    proxy = proxy_dir / (f.stem.replace("/", "_") + ".jpg")
    thumb = thumb_dir / proxy.name
    if f.jpg:
        img = Image.open(f.jpg)
        f.taken, f.taken_str = exif_time(img)
        f.width, f.height = img.size
        # DCT scaling: decode a 6000x4000 JPEG at 1/4 size. Several times faster than a full decode.
        img.draft("RGB", (PROXY_EDGE, PROXY_EDGE))
    else:
        img = open_raw_preview(f.raw)
        f.taken, f.taken_str = exif_time(img)
        f.width, f.height = img.size
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((PROXY_EDGE, PROXY_EDGE), Image.LANCZOS)
    img.save(proxy, "JPEG", quality=85, optimize=True)
    t = img.copy()
    t.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
    t.save(thumb, "JPEG", quality=78)

    gray = img.convert("L")
    small = gray.copy()
    small.thumbnail((512, 512), Image.BILINEAR)
    arr = np.asarray(small)
    f.sharp = laplacian_var(arr)
    full = np.asarray(gray)
    f.clip_hi = float((full >= 254).mean())
    f.clip_lo = float((full <= 3).mean())
    f.dhash = dhash(gray)
    if not f.taken:
        try:
            f.taken = Path(f.source).stat().st_mtime
        except OSError:
            f.taken = 0.0
    return f


# --------------------------------------------------------------------------- local pass


def local_pass(frames: list[Frame], keep_per_burst: int, no_local_reject: bool) -> None:
    frames.sort(key=lambda f: (f.taken, f.stem))
    # Bursts: frames under ~1s apart that also look alike. Both conditions, so a quick
    # sequence that changes scene (shot then celebration) still splits.
    gid = 0
    prev = None
    for f in frames:
        if prev is None or f.taken - prev.taken > 1.0 or bin(f.dhash ^ prev.dhash).count("1") > 12:
            gid += 1
        f.burst = gid
        prev = f

    sharp = np.array([f.sharp for f in frames]) if frames else np.array([0.0])
    median = float(np.median(sharp)) or 1.0
    if not no_local_reject:
        for f in frames:
            if f.sharp < 0.18 * median:
                f.status, f.local_reason = "local_reject", "soft_focus"
            elif f.clip_hi > 0.40:
                f.status, f.local_reason = "local_reject", "blown_highlights"
            elif f.clip_lo > 0.60:
                f.status, f.local_reason = "local_reject", "underexposed"

    groups: dict[int, list[Frame]] = {}
    for f in frames:
        groups.setdefault(f.burst, []).append(f)
    for members in groups.values():
        alive = sorted([m for m in members if m.status != "local_reject"], key=lambda m: -m.sharp)
        for i, m in enumerate(alive):
            m.burst_rep = i < keep_per_burst
            if not m.burst_rep:
                m.status = "burst_dup"


# --------------------------------------------------------------------------- scoring


class Fatal(Exception):
    pass


def system_blocks(sport: str) -> list[dict]:
    return [
        {"type": "text", "text": RUBRIC},
        {"type": "text", "text": SPORT_NOTES[sport], "cache_control": {"type": "ephemeral"}},
    ]


def request_params(sport: str, b64: str, model: str) -> dict:
    return {
        "model": model,
        "max_tokens": 1024,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCORE_SCHEMA}},
        "system": system_blocks(sport),
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": USER_TEXT},
        ]}],
    }


def proxy_b64(proxy_dir: Path, f: Frame) -> str:
    return base64.standard_b64encode((proxy_dir / (f.stem.replace("/", "_") + ".jpg")).read_bytes()).decode()


def parse_scores(message) -> dict:
    if getattr(message, "stop_reason", None) == "refusal":
        raise ValueError("refused")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        raise ValueError("no text block")
    s = json.loads(text)
    for k in WEIGHTS:
        if s.get(k) not in (1, 2, 3, 4, 5):
            raise ValueError(f"bad {k}")
    return s


def apply_scores(f: Frame, s: dict | None) -> None:
    if s is None:                       # unscored is not zero
        f.status, f.scores, f.weighted, f.tier = "unscored", None, None, ""
        return
    f.scores = s
    f.weighted = round(sum(s[k] * w for k, w in WEIGHTS.items()), 2)
    tier = "REJECT"
    for name, cut in TIERS:
        if f.weighted >= cut:
            tier = name
            break
    if s.get("reject_reason", "none") != "none":
        tier = "REJECT"
    f.tier, f.status = tier, "scored"


class Usage:
    def __init__(self):
        self.lock = threading.Lock()
        self.inp = self.out = self.cache_r = self.cache_w = 0

    def add(self, u) -> None:
        if u is None:
            return
        with self.lock:
            self.inp += getattr(u, "input_tokens", 0) or 0
            self.out += getattr(u, "output_tokens", 0) or 0
            self.cache_r += getattr(u, "cache_read_input_tokens", 0) or 0
            self.cache_w += getattr(u, "cache_creation_input_tokens", 0) or 0

    def dollars(self, batch: bool) -> float:
        d = (self.inp * PRICE_IN + self.cache_r * PRICE_IN * 0.1
             + self.cache_w * PRICE_IN * 1.25 + self.out * PRICE_OUT) / 1e6
        return d * (0.5 if batch else 1.0)


def is_fatal(e: Exception) -> str | None:
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "API key rejected. Check ANTHROPIC_API_KEY in your .env."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "API key lacks permission for this model."
    if isinstance(e, anthropic.BadRequestError) and "credit" in str(e).lower():
        return "Out of API credits. Top up at platform.claude.com."
    if isinstance(e, anthropic.APIConnectionError):
        return "Can't reach api.anthropic.com. Check the network."
    return None


def score_realtime(client, todo: list[Frame], proxy_dir: Path, sport: str, model: str,
                   workers: int, cache: "ScoreCache", usage: Usage) -> None:
    done = 0
    consecutive_fail = 0
    stop = threading.Event()

    def one(f: Frame):
        if stop.is_set():
            return f, None, None
        params = request_params(sport, proxy_b64(proxy_dir, f), model)
        msg = client.messages.create(
            **params,
            # Server side refusal fallback: if Opus 5 declines a frame, the API reruns it
            # on a fallback model inside the same call instead of returning nothing.
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        usage.add(msg.usage)
        return f, parse_scores(msg), None

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, f): f for f in todo}
        for fut in cf.as_completed(futs):
            f = futs[fut]
            try:
                _, s, _ = fut.result()
                if s is None:
                    continue
                apply_scores(f, s)
                cache.put(f)
                consecutive_fail = 0
            except Exception as e:
                why = is_fatal(e)
                f.status, f.error = "unscored", str(e)[:200]
                consecutive_fail += 1
                if why or consecutive_fail >= 6:
                    stop.set()
                    for x in futs:
                        x.cancel()
                    raise Fatal(why or f"Six frames in a row failed. Last error: {str(e)[:200]}")
            done += 1
            if done % 10 == 0 or done == len(todo):
                log(f"  scored {done}/{len(todo)}   spent so far ${usage.dollars(False):.2f}")


def score_batch(client, todo: list[Frame], proxy_dir: Path, sport: str, model: str,
                cache: "ScoreCache", usage: Usage, state_path: Path) -> None:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    by_id = {}
    state = json.loads(state_path.read_text()) if state_path.exists() else {"batches": []}

    # Submit in chunks that stay well under the 256 MB batch limit.
    pending_ids = set(state.get("submitted_keys", []))
    chunk, chunk_bytes, chunks = [], 0, []
    for i, f in enumerate(todo):
        cid = f"f{i:05d}"
        by_id[cid] = f
        if f.key in pending_ids:
            continue
        b64 = proxy_b64(proxy_dir, f)
        req = Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**request_params(sport, b64, model)))
        if chunk and chunk_bytes + len(b64) > 180 * 1024 * 1024:
            chunks.append(chunk)
            chunk, chunk_bytes = [], 0
        chunk.append((req, f.key))
        chunk_bytes += len(b64)
    if chunk:
        chunks.append(chunk)

    for c in chunks:
        b = client.messages.batches.create(requests=[r for r, _ in c])
        state["batches"].append({"id": b.id, "map": {r["custom_id"]: k for r, k in c}})
        state.setdefault("submitted_keys", []).extend(k for _, k in c)
        state_path.write_text(json.dumps(state))
        log(f"  submitted batch {b.id} with {len(c)} frames")

    key_to_frame = {f.key: f for f in todo}
    for entry in state["batches"]:
        if entry.get("collected"):
            continue
        log(f"  waiting on {entry['id']} (most finish within the hour; Ctrl+C is safe, rerun to resume)")
        while True:
            b = client.messages.batches.retrieve(entry["id"])
            if b.processing_status == "ended":
                break
            rc = b.request_counts
            log(f"    processing {rc.processing}, done {rc.succeeded + rc.errored}")
            time.sleep(30)
        for r in client.messages.batches.results(entry["id"]):
            f = key_to_frame.get(entry["map"].get(r.custom_id))
            if f is None:
                continue
            if r.result.type == "succeeded":
                usage.add(r.result.message.usage)
                try:
                    apply_scores(f, parse_scores(r.result.message))
                    cache.put(f)
                except Exception as e:
                    apply_scores(f, None)
                    f.error = str(e)[:200]
            else:
                apply_scores(f, None)
                f.error = r.result.type
        entry["collected"] = True
        state_path.write_text(json.dumps(state))


class ScoreCache:
    """Append only JSONL. A rerun on the same card never pays for a frame twice."""

    def __init__(self, path: Path):
        self.path, self.lock, self.data = path, threading.Lock(), {}
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    self.data[row["key"]] = row["scores"]
                except Exception:
                    pass

    def get(self, f: Frame):
        return self.data.get(f.key)

    def put(self, f: Frame) -> None:
        with self.lock:
            self.data[f.key] = f.scores
            with self.path.open("a") as fh:
                fh.write(json.dumps({"key": f.key, "scores": f.scores}) + "\n")


def estimate(client, sample: Frame, proxy_dir: Path, sport: str, model: str, n: int, batch: bool) -> float:
    p = request_params(sport, proxy_b64(proxy_dir, sample), model)
    try:
        tok = client.messages.count_tokens(model=model, system=p["system"], messages=p["messages"]).input_tokens
    except Exception:
        tok = 933 + 1500
    rubric_tok = 1500
    image_tok = max(tok - rubric_tok, 300)
    per = (image_tok * PRICE_IN + rubric_tok * PRICE_IN * 0.1 + 180 * PRICE_OUT) / 1e6
    return per * n * (0.5 if batch else 1.0)


# --------------------------------------------------------------------------- outputs


def write_xmp(dst: Path, tier: str, f: Frame) -> None:
    rating = XMP_RATING.get(tier, 0)
    label = {"HERO": "Red", "KEEPER": "Green", "MAYBE": "Yellow"}.get(tier, "")
    kw = "".join(f"<rdf:li>{html.escape(k)}</rdf:li>" for k in
                 ["CullPro " + tier] + [f"sponsor:{s}" for s in (f.scores or {}).get("sponsors_visible", [])])
    dst.write_text(f"""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmp:Rating="{rating}" xmp:Label="{label}"><dc:subject><rdf:Bag>{kw}</rdf:Bag></dc:subject></rdf:Description>
</rdf:RDF></x:xmpmeta>
""")


def copy_picks(frames: list[Frame], out: Path, tiers: set[str]) -> int:
    n = 0
    for f in frames:
        if f.tier not in tiers:
            continue
        dest = out / "PICKS" / f.tier
        dest.mkdir(parents=True, exist_ok=True)
        base = f.stem.replace("/", "_")
        for src in (f.jpg, f.raw):
            if not src:
                continue
            target = dest / (base + Path(src).suffix)
            try:
                if not target.exists() or target.stat().st_size != Path(src).stat().st_size:
                    shutil.copy2(src, target)
            except OSError as e:
                log(f"  couldn't copy {src}: {e.strerror or e}")
        if f.raw:  # Lightroom reads sidecar ratings for RAW files on import
            write_xmp(dest / (base + ".xmp"), f.tier, f)
        n += 1
    return n


def write_csv(frames: list[Frame], path: Path) -> None:
    cols = ["file", "tier", "weighted", *WEIGHTS, "shot_type", "reject_reason", "sponsors_visible",
            "status", "local_reason", "burst", "sharpness", "taken", "error"]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in sorted(frames, key=lambda f: -(f.weighted if f.weighted is not None else -1)):
            s = f.scores or {}
            w.writerow([f.stem, f.tier, "" if f.weighted is None else f.weighted,
                        *[s.get(k, "") for k in WEIGHTS], s.get("shot_type", ""), s.get("reject_reason", ""),
                        "; ".join(s.get("sponsors_visible", [])), f.status, f.local_reason, f.burst,
                        round(f.sharp, 1), f.taken_str, f.error])


def write_report(frames: list[Frame], out: Path, meta: dict) -> Path:
    order = {"HERO": 0, "KEEPER": 1, "MAYBE": 2, "REJECT": 3, "": 4}
    shown = sorted(frames, key=lambda f: (order.get(f.tier, 4), -(f.weighted or 0), f.taken))
    counts = {t: sum(1 for f in frames if f.tier == t) for t in ("HERO", "KEEPER", "MAYBE", "REJECT")}
    counts["dup"] = sum(1 for f in frames if f.status == "burst_dup")
    counts["local"] = sum(1 for f in frames if f.status == "local_reject")
    counts["unscored"] = sum(1 for f in frames if f.status == "unscored")

    cards = []
    for f in shown:
        s = f.scores or {}
        cls = (f.tier or {"burst_dup": "DUP", "local_reject": "LOCAL", "unscored": "UNSCORED"}.get(f.status, "PENDING"))
        dims = "".join(f"<span>{k[:4]} {s[k]}</span>" for k in WEIGHTS) if s else ""
        note = ""
        if f.status == "burst_dup":
            note = f"burst {f.burst}, sharper twin scored"
        elif f.status == "local_reject":
            note = f"local reject: {f.local_reason.replace('_', ' ')}"
        elif f.status == "unscored":
            note = "unscored (request failed)"
        elif s.get("reject_reason", "none") != "none":
            note = s["reject_reason"].replace("_", " ")
        spons = ", ".join(s.get("sponsors_visible", []))
        cards.append(
            f'<figure class="c" data-t="{cls}"><img loading="lazy" src="_thumbs/{html.escape(f.stem.replace("/", "_"))}.jpg" alt="{html.escape(f.stem)}">'
            f'<figcaption><b class="t {cls}">{cls}</b> <b>{"" if f.weighted is None else f.weighted}</b> '
            f'<code>{html.escape(f.stem)}</code><div class="d">{dims}</div>'
            f'{f"<div class=n>{html.escape(note)}</div>" if note else ""}'
            f'{f"<div class=s>{html.escape(spons)}</div>" if spons else ""}</figcaption></figure>')

    btns = "".join(f'<button data-f="{t}">{t} <i>{n}</i></button>' for t, n in [
        ("HERO", counts["HERO"]), ("KEEPER", counts["KEEPER"]), ("MAYBE", counts["MAYBE"]),
        ("REJECT", counts["REJECT"]), ("DUP", counts["dup"]), ("LOCAL", counts["local"]),
        ("UNSCORED", counts["unscored"])])
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CullPro {html.escape(meta['session'])}</title>
<style>
:root{{--bg:#fff;--ink:#141414;--mute:#5c5c5c;--line:#d9d9d9;--g:#73000a;--hero:#73000a;--keep:#1d6b3a;--maybe:#8a6d00;--rej:#5c5c5c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--ink:#f2f2f2;--mute:#a8a8a8;--line:#333;--hero:#ff6b6b;--keep:#6fd39a;--maybe:#e6c55a;--rej:#a8a8a8}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,sans-serif}}
header{{border-top:6px solid var(--g);padding:20px 16px 8px}}h1{{margin:0 0 4px;font-size:22px}}
.m{{color:var(--mute)}}nav{{display:flex;flex-wrap:wrap;gap:6px;padding:8px 16px;position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line)}}
button{{font:inherit;padding:6px 10px;border:1px solid var(--line);background:none;color:var(--ink);cursor:pointer;border-radius:0}}
button[aria-pressed=true]{{background:var(--ink);color:var(--bg)}}button i{{font-style:normal;color:var(--mute)}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;padding:16px}}
.c{{margin:0}}.c img{{width:100%;display:block;background:var(--line);aspect-ratio:3/2;object-fit:cover}}
figcaption{{padding:6px 0;font-size:12.5px}}code{{color:var(--mute)}}.d{{display:flex;flex-wrap:wrap;gap:6px;color:var(--mute)}}
.n{{color:var(--maybe)}}.s{{color:var(--keep)}}.t.HERO{{color:var(--hero)}}.t.KEEPER{{color:var(--keep)}}.t.MAYBE{{color:var(--maybe)}}.t.REJECT,.t.DUP,.t.LOCAL,.t.UNSCORED{{color:var(--rej)}}
</style></head><body>
<header><h1>{html.escape(meta['session'])}</h1><div class="m">{meta['total']} frames on card &middot; {meta['scored']} scored &middot; {meta['saved']} skipped for free &middot; ${meta['cost']:.2f} {meta['mode']} &middot; sport: {meta['sport']}</div></header>
<nav><button data-f="ALL" aria-pressed="true">All</button>{btns}</nav>
<main>{''.join(cards)}</main>
<script>
const b=[...document.querySelectorAll('nav button')],c=[...document.querySelectorAll('.c')];
b.forEach(x=>x.onclick=()=>{{b.forEach(y=>y.setAttribute('aria-pressed',y===x));const f=x.dataset.f;c.forEach(k=>k.hidden=f!=='ALL'&&k.dataset.t!==f)}});
</script></body></html>"""
    path = out / "report.html"
    path.write_text(page, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="Cull a gameday card straight off the SD card.")
    ap.add_argument("folder", nargs="?", help="folder dropped onto the launcher")
    ap.add_argument("--card", help="card or folder path (default: ask)")
    ap.add_argument("--out", help="session folder (default: ~/Pictures/CullPro/<date>_<card>)")
    ap.add_argument("--sport", choices=list(SPORT_NOTES), default="hockey")
    ap.add_argument("--batch", action="store_true", help="Batch API: 50%% cheaper, not instant")
    ap.add_argument("--dry-run", action="store_true", help="local pass only, no API calls")
    ap.add_argument("--trial", type=int, default=0, help="score only the first N frames to check cost")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--keep-per-burst", type=int, default=1, help="frames scored per burst (default 1)")
    ap.add_argument("--no-local-reject", action="store_true", help="send blurry/clipped frames too")
    ap.add_argument("--picks", default="HERO,KEEPER", help="tiers copied off the card (default HERO,KEEPER)")
    ap.add_argument("--workers", type=int, default=8, help="parallel requests in realtime mode")
    ap.add_argument("--read-threads", type=int, default=4, help="parallel card reads/decodes")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--no-open", action="store_true", help="don't open the report when done")
    a = ap.parse_args()

    t0 = time.time()
    card = find_card(a.card or a.folder)
    log(f"Card: {card}  (read only)")
    frames = scan_card(card)
    if not frames:
        sys.exit("No JPG or RAW files on that card.")
    n_raw = sum(1 for f in frames if f.raw)
    log(f"Found {len(frames)} frames ({n_raw} with RAW)")

    # Session folder named after the shoot date in EXIF, so the same card always maps here.
    first = min(frames, key=lambda f: f.stem)
    try:
        d = exif_time(Image.open(first.source) if first.jpg else open_raw_preview(first.raw))[1][:10].replace(":", "-")
    except Exception:
        d = ""
    session = f"{d or datetime.now().strftime('%Y-%m-%d')}_{card.name or 'card'}"
    out = Path(a.out).expanduser() if a.out else Path.home() / "Pictures" / "CullPro" / session
    proxy_dir, thumb_dir = out / ".proxies", out / "_thumbs"
    for p in (proxy_dir, thumb_dir):
        p.mkdir(parents=True, exist_ok=True)
    log(f"Session: {out}")

    # 1. one read per file off the card: proxy, thumb, sharpness, exposure, hash
    meta_path = out / ".frames.json"
    known = {}
    if meta_path.exists():
        known = {r["key"]: r for r in json.loads(meta_path.read_text())}
    todo = []
    for f in frames:
        k = known.get(f.key)
        if k and (proxy_dir / (f.stem.replace("/", "_") + ".jpg")).exists():
            for fld in ("taken", "taken_str", "width", "height", "sharp", "clip_hi", "clip_lo", "dhash"):
                setattr(f, fld, k[fld])
        else:
            todo.append(f)
    if todo:
        log(f"Reading {len(todo)} frames off the card...")
        bad = []
        with cf.ThreadPoolExecutor(max_workers=a.read_threads) as pool:
            futs = {pool.submit(make_proxy, f, proxy_dir, thumb_dir): f for f in todo}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                try:
                    fut.result()
                except Exception as e:
                    f = futs[fut]
                    f.status, f.error = "error", f"unreadable: {e}"[:200]
                    bad.append(f)
                if i % 50 == 0 or i == len(todo):
                    rate = i / max(time.time() - t0, 0.01)
                    log(f"  {i}/{len(todo)}  ({rate:.0f}/s)")
        if bad:
            log(f"  {len(bad)} files could not be read and are listed as errors")
    frames = [f for f in frames if f.status != "error"] + [f for f in frames if f.status == "error"]
    readable = [f for f in frames if f.status != "error"]
    meta_path.write_text(json.dumps([{"key": f.key, **{k: getattr(f, k) for k in (
        "taken", "taken_str", "width", "height", "sharp", "clip_hi", "clip_lo", "dhash")}} for f in readable]))

    # 2. free local pass
    local_pass(readable, max(1, a.keep_per_burst), a.no_local_reject)
    n_dup = sum(1 for f in readable if f.status == "burst_dup")
    n_loc = sum(1 for f in readable if f.status == "local_reject")
    log(f"Local pass: {n_dup} burst duplicates and {n_loc} blurry/clipped frames skipped for free")

    # 3. score
    cache = ScoreCache(out / "scores.jsonl")
    need = []
    for f in readable:
        if f.status != "pending":
            continue
        hit = cache.get(f)
        if hit:
            apply_scores(f, hit)
        else:
            need.append(f)
    if a.trial:
        need = need[: a.trial]
    already = sum(1 for f in readable if f.status == "scored")
    log(f"To score: {len(need)}  (already scored on an earlier run: {already})")

    usage = Usage()
    if need and not a.dry_run:
        import anthropic
        env_file = Path(__file__).resolve().parent / ".env"
        key = find_api_key()
        while True:
            if not key:
                log("\nPaste your Claude API key (right click in this window), then press Enter.")
                log("Get one at platform.claude.com > API keys > Create key. It starts with sk-ant-")
                key = input("Key: ").strip().strip('"').strip("'")
                if not key.startswith("sk-ant-") or "..." in key or len(key) < 40:
                    log("That isn't a whole key. Click Copy next to the key on the website and paste again.")
                    key = None
                    continue
            log("Checking the key...")
            client = anthropic.Anthropic(api_key=key, max_retries=2)
            try:
                client.messages.count_tokens(model=a.model, messages=[{"role": "user", "content": "hi"}])
                env_file.write_text(f"ANTHROPIC_API_KEY={key}\n")
                log("Key works. Saved, you won't be asked again.\n")
                break
            except anthropic.AuthenticationError:
                log("That key didn't work (deleted, expired, or mistyped). Paste a new one.")
                if env_file.exists():
                    env_file.unlink()
                key = None
            except anthropic.APIConnectionError:
                sys.exit("Can't reach the internet. Check Wi-Fi and run again.")
        client = anthropic.Anthropic(api_key=key, max_retries=4)
        est = estimate(client, need[0], proxy_dir, a.sport, a.model, len(need), a.batch)
        log(f"Estimated cost: about ${est:.2f} for {len(need)} frames "
            f"({'Batch API, half price' if a.batch else 'realtime'})")
        if not a.yes and input("Go? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Stopped before spending anything. Local results are saved; rerun anytime.")
        try:
            if a.batch:
                score_batch(client, need, proxy_dir, a.sport, a.model, cache, usage, out / ".batch_state.json")
            else:
                score_realtime(client, need, proxy_dir, a.sport, a.model, a.workers, cache, usage)
        except Fatal as e:
            log(f"\nStopped: {e}\nEverything scored so far is saved. Fix the cause and rerun.")
        except KeyboardInterrupt:
            log("\nInterrupted. Scores so far are saved; rerun to continue.")

    # 4. outputs
    picks = set(t.strip().upper() for t in a.picks.split(",") if t.strip())
    n_picks = 0 if a.dry_run else copy_picks(readable, out, picks)
    write_csv(frames, out / "scores.csv")
    scored = sum(1 for f in readable if f.status == "scored")
    report = write_report(frames, out, {
        "session": session, "total": len(frames), "scored": scored, "saved": n_dup + n_loc,
        "cost": usage.dollars(a.batch), "mode": "batch" if a.batch else "realtime", "sport": a.sport})

    tiers = {t: sum(1 for f in readable if f.tier == t) for t in ("HERO", "KEEPER", "MAYBE", "REJECT")}
    log("")
    log(f"HERO {tiers['HERO']}   KEEPER {tiers['KEEPER']}   MAYBE {tiers['MAYBE']}   REJECT {tiers['REJECT']}")
    if usage.inp or usage.out:
        log(f"Spent ${usage.dollars(a.batch):.2f}  (cache hits on {usage.cache_r:,} rubric tokens)")
    if n_picks:
        log(f"Copied {n_picks} picks off the card into {out / 'PICKS'}")
    log(f"Report: {report}")
    log(f"Done in {time.time() - t0:.0f}s")
    if not a.no_open:
        try:
            webbrowser.open(report.as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
