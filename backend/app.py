"""Conservation Video Classifier — FastAPI backend.

Serves the static SPA and a JSON API for uploading videos, running multi-model
analysis through OpenRouter, computing consensus, and saving human reviews.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import llm, media

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
FRAMES = DATA / "frames"
THUMBS = DATA / "thumbs"
COMPRESSED = DATA / "compressed"
STATIC = ROOT / "static"
DB_PATH = DATA / "app.db"

for d in (UPLOADS, FRAMES, THUMBS, COMPRESSED):
    d.mkdir(parents=True, exist_ok=True)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader — populates os.environ for keys not already set."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and os.environ.get(key) is None:
            os.environ[key] = val


load_dotenv()

# "Auto" mode: a fixed, curated set the team can rely on without configuring anything.
AUTO_MODELS = [
    "perceptron/perceptron-mk1",
    "google/gemini-2.5-flash",
    "qwen/qwen3.6-plus",
    "z-ai/glm-4.6v",
]
DEFAULT_MODELS = list(AUTO_MODELS)        # default for Custom mode picker
DEFAULT_FRAME_COUNT = 16                  # denser sampling helps behavior accuracy
MAX_BATCH_ANALYZE = 10                    # cap clips analyzed in one batch
DEFAULT_REVIEW_POLICY = "majority"        # "majority" (auto-accept on majority) | "unanimous"
DEFAULT_INPUT_MODE = "video"              # "video" (native clip) | "frames" (sampled stills)


def input_mode() -> str:
    return get_setting("input_mode", DEFAULT_INPUT_MODE)


def get_compressed_video(video_id: str, stored_name: str) -> Path | None:
    """Return a small mp4 for native video input, transcoding+caching on first use."""
    dest = COMPRESSED / f"{video_id}.mp4"
    if dest.exists():
        return dest
    return media.compress_for_upload(UPLOADS / stored_name, dest)


def review_policy() -> str:
    return get_setting("review_policy", DEFAULT_REVIEW_POLICY)


def workflow_status(n_analyses: int, consensus: dict, has_review: bool) -> str:
    """The clip's place in the triage flow: not_analyzed | needs_review | confident | reviewed."""
    if has_review:
        return "reviewed"
    if not n_analyses:
        return "not_analyzed"
    return "needs_review" if consensus.get("needs_review") else "confident"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # Wait (don't error) if the file is briefly locked under concurrent batch analysis.
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                original_name TEXT,
                stored_name TEXT,
                duration REAL, width INTEGER, height INTEGER, size_bytes INTEGER,
                status TEXT DEFAULT 'uploaded',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                model TEXT,
                status TEXT,
                species_common TEXT, species_scientific TEXT,
                behavior TEXT, behavior_detail TEXT,
                count INTEGER, confidence REAL, notes TEXT,
                prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
                cost_usd REAL, latency_ms INTEGER,
                error TEXT, raw_text TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS reviews (
                video_id TEXT PRIMARY KEY,
                decision TEXT,
                final_species_common TEXT, final_species_scientific TEXT,
                final_behavior TEXT, notes TEXT,
                reviewer TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            """
        )


def get_setting(key: str, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


PLACEHOLDER_KEYS = {"", "sk-or-v1-your-key-here"}


def get_api_key() -> str | None:
    """Read the OpenRouter key from the environment (loaded from .env)."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    return key if key not in PLACEHOLDER_KEYS else None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Conservation Video Classifier")
init_db()


def video_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "original_name": row["original_name"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "size_bytes": row["size_bytes"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def analysis_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("raw_text", None)  # keep payloads small in list views
    return d


def get_video_payload(video_id: str) -> dict | None:
    with db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not v:
            return None
        analyses = conn.execute(
            "SELECT * FROM analyses WHERE video_id=? ORDER BY created_at", (video_id,)
        ).fetchall()
        review = conn.execute(
            "SELECT * FROM reviews WHERE video_id=?", (video_id,)
        ).fetchone()

    preds = [analysis_row_to_dict(a) for a in analyses]
    consensus = llm.compute_consensus([dict(a) for a in analyses], review_policy())
    totals = {
        "cost_usd": round(sum((a["cost_usd"] or 0) for a in analyses), 6),
        "total_tokens": sum((a["total_tokens"] or 0) for a in analyses),
        "prompt_tokens": sum((a["prompt_tokens"] or 0) for a in analyses),
        "completion_tokens": sum((a["completion_tokens"] or 0) for a in analyses),
    }
    return {
        "video": video_row_to_dict(v),
        "analyses": preds,
        "consensus": consensus,
        "totals": totals,
        "review": dict(review) if review else None,
        "workflow_status": workflow_status(len(analyses), consensus, review is not None),
    }


# ----------------------------- Settings ------------------------------------
def effective_models() -> list[str]:
    """Models actually used for analysis, based on the current mode."""
    mode = get_setting("mode", "auto")
    if mode == "custom":
        return get_setting("selected_models", DEFAULT_MODELS)
    return list(AUTO_MODELS)


@app.get("/api/settings")
def read_settings():
    return {
        "has_api_key": bool(get_api_key()),
        "mode": get_setting("mode", "auto"),
        "auto_models": list(AUTO_MODELS),
        "selected_models": get_setting("selected_models", DEFAULT_MODELS),
        "effective_models": effective_models(),
        "frame_count": get_setting("frame_count", DEFAULT_FRAME_COUNT),
        "max_batch": MAX_BATCH_ANALYZE,
        "review_policy": review_policy(),
        "input_mode": input_mode(),
    }


@app.post("/api/settings")
async def write_settings(request: Request):
    # The API key is read from .env only; it is never accepted over HTTP.
    body = await request.json()
    if body.get("mode") in ("auto", "custom"):
        set_setting("mode", body["mode"])
    if body.get("review_policy") in ("majority", "unanimous"):
        set_setting("review_policy", body["review_policy"])
    if body.get("input_mode") in ("video", "frames"):
        set_setting("input_mode", body["input_mode"])
    if "selected_models" in body and isinstance(body["selected_models"], list):
        set_setting("selected_models", body["selected_models"])
    if "frame_count" in body:
        try:
            fc = max(4, min(24, int(body["frame_count"])))
            set_setting("frame_count", fc)
        except Exception:
            pass
    return read_settings()


@app.get("/api/models")
async def models():
    api_key = get_api_key()
    if not api_key:
        raise HTTPException(400, "No OpenRouter API key configured")
    try:
        items = await llm.list_video_models(api_key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Failed to fetch models: {e}")
    return {"models": items}


# ----------------------------- Videos --------------------------------------
@app.post("/api/videos")
async def upload_videos(files: list[UploadFile] = File(...)):
    created = []
    frame_count = get_setting("frame_count", DEFAULT_FRAME_COUNT)
    for f in files:
        vid = uuid.uuid4().hex
        ext = Path(f.filename or "video.mp4").suffix or ".mp4"
        stored = f"{vid}{ext}"
        dest = UPLOADS / stored
        with open(dest, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)

        meta = media.probe(dest)
        with db() as conn:
            conn.execute(
                "INSERT INTO videos(id, original_name, stored_name, duration, width, "
                "height, size_bytes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (vid, f.filename, stored, meta["duration"], meta["width"],
                 meta["height"], meta["size_bytes"], "uploaded", now_iso()),
            )
        # Thumbnail + frame extraction off the event loop.
        await asyncio.to_thread(media.make_thumbnail, dest, THUMBS / f"{vid}.jpg",
                                min(1.0, meta["duration"] / 2))
        await asyncio.to_thread(media.extract_frames, dest, FRAMES / vid,
                                frame_count, 768, meta["duration"])
        created.append(vid)

    return {"created": created}


@app.get("/api/videos")
def list_videos():
    policy = review_policy()
    with db() as conn:
        rows = conn.execute("SELECT * FROM videos ORDER BY created_at DESC").fetchall()
        out = []
        for v in rows:
            analyses = conn.execute(
                "SELECT * FROM analyses WHERE video_id=?", (v["id"],)
            ).fetchall()
            review = conn.execute(
                "SELECT decision, final_species_common FROM reviews WHERE video_id=?", (v["id"],)
            ).fetchone()
            consensus = llm.compute_consensus([dict(a) for a in analyses], policy)
            d = video_row_to_dict(v)
            d["consensus"] = consensus
            d["n_analyses"] = len(analyses)
            d["review_decision"] = review["decision"] if review else None
            d["final_species"] = review["final_species_common"] if review else None
            d["workflow_status"] = workflow_status(len(analyses), consensus, review is not None)
            d["cost_usd"] = round(sum((a["cost_usd"] or 0) for a in analyses), 6)
            out.append(d)
    return {"videos": out}


@app.get("/api/videos/{video_id}")
def get_video(video_id: str):
    payload = get_video_payload(video_id)
    if not payload:
        raise HTTPException(404, "Video not found")
    return payload


@app.get("/api/videos/{video_id}/file")
def video_file(video_id: str):
    with db() as conn:
        row = conn.execute("SELECT stored_name FROM videos WHERE id=?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Video not found")
    path = UPLOADS / row["stored_name"]
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path)


@app.get("/api/videos/{video_id}/thumb")
def video_thumb(video_id: str):
    path = THUMBS / f"{video_id}.jpg"
    if not path.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(path)


@app.get("/api/videos/{video_id}/frames")
def video_frames(video_id: str):
    """List available frame indices for a video (for the UI frame strip)."""
    fdir = FRAMES / video_id
    if not fdir.exists():
        return {"frames": []}
    return {"frames": sorted(p.name for p in fdir.glob("frame_*.jpg"))}


@app.get("/api/videos/{video_id}/frames/{name}")
def video_frame(video_id: str, name: str):
    if not name.startswith("frame_") or "/" in name or ".." in name:
        raise HTTPException(400, "Bad frame name")
    path = FRAMES / video_id / name
    if not path.exists():
        raise HTTPException(404, "No such frame")
    return FileResponse(path)


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str):
    with db() as conn:
        row = conn.execute("SELECT stored_name FROM videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Video not found")
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
        conn.execute("DELETE FROM analyses WHERE video_id=?", (video_id,))
        conn.execute("DELETE FROM reviews WHERE video_id=?", (video_id,))
    (UPLOADS / row["stored_name"]).unlink(missing_ok=True)
    (THUMBS / f"{video_id}.jpg").unlink(missing_ok=True)
    (COMPRESSED / f"{video_id}.mp4").unlink(missing_ok=True)
    fdir = FRAMES / video_id
    if fdir.exists():
        for p in fdir.glob("*"):
            p.unlink(missing_ok=True)
        fdir.rmdir()
    return {"deleted": video_id}


# ----------------------------- Analysis ------------------------------------
@app.post("/api/videos/{video_id}/analyze")
async def analyze_video(video_id: str, request: Request):
    api_key = get_api_key()
    if not api_key:
        raise HTTPException(400, "No OpenRouter API key configured")

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    selected = body.get("models") or effective_models()
    if not selected:
        raise HTTPException(400, "No models selected")

    with db() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
    if not v:
        raise HTTPException(404, "Video not found")

    mode = input_mode()
    video_data_uri = None
    data_uris = None
    frame_labels = None

    if mode == "video":
        # Native video input: send the (compressed) clip itself.
        comp = await asyncio.to_thread(get_compressed_video, video_id, v["stored_name"])
        if not comp:
            raise HTTPException(500, "Could not prepare video for analysis")
        video_data_uri = await asyncio.to_thread(media.video_as_data_uri, comp)
    else:
        # Frame-sampling fallback.
        fdir = FRAMES / video_id
        frame_paths = sorted(fdir.glob("frame_*.jpg")) if fdir.exists() else []
        if not frame_paths:
            frame_count = get_setting("frame_count", DEFAULT_FRAME_COUNT)
            frame_paths = await asyncio.to_thread(
                media.extract_frames, UPLOADS / v["stored_name"], fdir,
                frame_count, 768, v["duration"],
            )
        if not frame_paths:
            raise HTTPException(500, "Could not extract frames from video")
        data_uris = media.frames_as_data_uris(frame_paths)
        n = len(frame_paths)
        dur = v["duration"] or 0.0
        frame_labels = [
            f"Frame {i + 1} of {n} (t≈{dur * (i + 0.5) / n:.1f}s)" for i in range(n)
        ]

    with db() as conn:
        conn.execute("UPDATE videos SET status='processing' WHERE id=?", (video_id,))
        # A re-run is a completely fresh run: drop prior analyses AND the prior human
        # review. During the experimentation phase the team is trying different model
        # combos, so a re-analysis should not carry over an old "reviewed" verdict.
        # (Future: make review feedback persistent in a DB — see ROADMAP.md.)
        conn.execute("DELETE FROM analyses WHERE video_id=?", (video_id,))
        conn.execute("DELETE FROM reviews WHERE video_id=?", (video_id,))

    async def run_one(model: str):
        res = await llm.analyze_with_model(
            api_key, model, image_data_uris=data_uris,
            frame_labels=frame_labels, video_data_uri=video_data_uri,
        )
        with db() as conn:
            conn.execute(
                "INSERT INTO analyses(id, video_id, model, status, species_common, "
                "species_scientific, behavior, behavior_detail, count, confidence, notes, "
                "prompt_tokens, completion_tokens, total_tokens, cost_usd, latency_ms, "
                "error, raw_text, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, video_id, model, res["status"], res["species_common"],
                 res["species_scientific"], res["behavior"], res["behavior_detail"],
                 res["count"], res["confidence"], res["notes"], res["prompt_tokens"],
                 res["completion_tokens"], res["total_tokens"], res["cost_usd"],
                 res["latency_ms"], res["error"], res["raw_text"], now_iso()),
            )
        return res

    await asyncio.gather(*(run_one(m) for m in selected))

    with db() as conn:
        conn.execute("UPDATE videos SET status='done' WHERE id=?", (video_id,))

    return get_video_payload(video_id)


@app.post("/api/analyze-batch")
async def analyze_batch(request: Request):
    """Analyze multiple videos (sequentially per video, models in parallel)."""
    body = await request.json()
    ids = body.get("video_ids") or []
    models_sel = body.get("models")
    results = {}
    for vid in ids:
        try:
            payload = await analyze_video(vid, _FakeReq({"models": models_sel}))
            results[vid] = "done"
        except HTTPException as e:
            results[vid] = f"error: {e.detail}"
        except Exception as e:  # noqa: BLE001
            results[vid] = f"error: {e}"
    return {"results": results}


class _FakeReq:
    """Minimal Request-like wrapper to reuse analyze_video for batch."""
    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data


# ----------------------------- Reviews -------------------------------------
@app.post("/api/videos/{video_id}/review")
async def save_review(video_id: str, request: Request):
    body = await request.json()
    with db() as conn:
        if not conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone():
            raise HTTPException(404, "Video not found")
        conn.execute(
            "INSERT INTO reviews(video_id, decision, final_species_common, "
            "final_species_scientific, final_behavior, notes, reviewer, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET "
            "decision=excluded.decision, final_species_common=excluded.final_species_common, "
            "final_species_scientific=excluded.final_species_scientific, "
            "final_behavior=excluded.final_behavior, notes=excluded.notes, "
            "reviewer=excluded.reviewer, updated_at=excluded.updated_at",
            (video_id, body.get("decision"), body.get("final_species_common"),
             body.get("final_species_scientific"), body.get("final_behavior"),
             body.get("notes"), body.get("reviewer") or "reviewer", now_iso()),
        )
    return get_video_payload(video_id)


# ----------------------------- Scoreboard ----------------------------------
@app.get("/api/scoreboard")
def scoreboard():
    """Per-model accuracy vs. the human-reviewed ground truth.

    Computed on the fly: for every clip that has a saved review with a final species,
    compare each model's prediction to the human's final answer. This is the reward
    signal that a future adaptive (bandit) selector will learn from (see ROADMAP.md).
    """
    with db() as conn:
        reviews = conn.execute(
            "SELECT video_id, final_species_common, final_behavior FROM reviews "
            "WHERE final_species_common IS NOT NULL AND TRIM(final_species_common) != ''"
        ).fetchall()
        review_map = {r["video_id"]: r for r in reviews}
        if not review_map:
            return {"reviewed_videos": 0, "models": []}
        qmarks = ",".join("?" * len(review_map))
        rows = conn.execute(
            f"SELECT * FROM analyses WHERE video_id IN ({qmarks})",
            tuple(review_map.keys()),
        ).fetchall()

    agg: dict[str, dict] = {}
    for a in rows:
        rv = review_map[a["video_id"]]
        s = agg.setdefault(a["model"], {
            "model": a["model"], "n_scored": 0, "species_hits": 0,
            "behavior_scored": 0, "behavior_hits": 0, "errors": 0,
            "total_cost": 0.0, "total_tokens": 0, "latency_sum": 0, "latency_n": 0,
        })
        s["total_cost"] += a["cost_usd"] or 0
        s["total_tokens"] += a["total_tokens"] or 0
        if a["latency_ms"]:
            s["latency_sum"] += a["latency_ms"]
            s["latency_n"] += 1
        if a["status"] != "done":
            s["errors"] += 1
            continue
        s["n_scored"] += 1
        pred_sp = llm._norm(a["species_common"])
        if pred_sp and pred_sp == llm._norm(rv["final_species_common"]):
            s["species_hits"] += 1
        if rv["final_behavior"] and rv["final_behavior"].strip():
            s["behavior_scored"] += 1
            if llm._norm(a["behavior"]) == llm._norm(rv["final_behavior"]):
                s["behavior_hits"] += 1

    models = []
    for s in agg.values():
        n, bn = s["n_scored"], s["behavior_scored"]
        models.append({
            "model": s["model"],
            "n_scored": n,
            "errors": s["errors"],
            "species_hits": s["species_hits"],
            "species_match_rate": round(s["species_hits"] / n, 3) if n else None,
            "behavior_hits": s["behavior_hits"],
            "behavior_match_rate": round(s["behavior_hits"] / bn, 3) if bn else None,
            "total_cost": round(s["total_cost"], 6),
            "total_tokens": s["total_tokens"],
            "avg_latency_ms": int(s["latency_sum"] / s["latency_n"]) if s["latency_n"] else 0,
            "cost_per_correct": round(s["total_cost"] / s["species_hits"], 6) if s["species_hits"] else None,
        })
    # Best species accuracy first; ties broken by sample size then cost.
    models.sort(key=lambda x: (
        x["species_match_rate"] if x["species_match_rate"] is not None else -1,
        x["n_scored"], -(x["cost_per_correct"] or 0),
    ), reverse=True)
    return {"reviewed_videos": len(review_map), "models": models}


# ----------------------------- Export --------------------------------------
@app.get("/api/export.csv")
def export_csv():
    import csv
    import io

    with db() as conn:
        videos = conn.execute("SELECT * FROM videos ORDER BY created_at").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "video", "duration_s", "consensus_level", "flagged",
            "consensus_species", "consensus_behavior",
            "review_decision", "final_species", "final_behavior", "review_notes",
            "total_cost_usd", "total_tokens",
        ])
        for v in videos:
            analyses = conn.execute(
                "SELECT * FROM analyses WHERE video_id=?", (v["id"],)
            ).fetchall()
            review = conn.execute(
                "SELECT * FROM reviews WHERE video_id=?", (v["id"],)
            ).fetchone()
            c = llm.compute_consensus([dict(a) for a in analyses], review_policy())
            w.writerow([
                v["original_name"], v["duration"], c["level"], c["flagged"],
                c["majority_species"], c["majority_behavior"],
                review["decision"] if review else "",
                review["final_species_common"] if review else "",
                review["final_behavior"] if review else "",
                (review["notes"] if review else "") or "",
                round(sum((a["cost_usd"] or 0) for a in analyses), 6),
                sum((a["total_tokens"] or 0) for a in analyses),
            ])
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=classifications.csv"},
    )


# ----------------------------- Static SPA ----------------------------------
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
