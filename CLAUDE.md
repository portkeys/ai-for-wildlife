# CLAUDE.md

Guidance for working in this repo. Read this before making changes.

## What this is

**AI for Wildlife** — a prototype that helps Cheetah Conservation Fund (CCF) conservationists
use multimodal LLMs to identify animal **species + behavior** in field/camera-trap video.
Each clip is sent to several models at once (via OpenRouter), a **consensus** is computed, and
a human reviewer confirms or corrects the uncertain minority. Per-clip token usage and cost are
tracked throughout.

Audience note: users are **field biologists, not engineers**. Keep all UI copy jargon-free.

## Architecture

A single FastAPI process serves both the JSON API and the static frontend — same origin, no
separate frontend server, **no build step**.

- **Backend** — Python 3.11+ / FastAPI / SQLite / `ffmpeg`. Entry point: `backend.app:app`.
  - `backend/app.py` — all routes, SQLite schema/access, settings, consensus orchestration,
    scoreboard, CSV export. Mounts `static/` at `/` (so the API routes are under `/api/...`).
  - `backend/llm.py` — OpenRouter client: builds the wildlife-ID prompt, sends native video
    **or** sampled frames, parses/normalizes the JSON response, computes cross-model consensus.
  - `backend/media.py` — `ffmpeg`/`ffprobe` helpers: probe metadata, thumbnail, frame
    extraction, and transcode-to-small-mp4 for native video input.
- **Frontend** — zero-build static SPA in `static/` (`index.html`, `app.js`, `styles.css`).
  Plain ES, no framework, no `npm`. Talks to the backend with **relative** paths (`/api/...`),
  so frontend and backend must be served from the **same origin**.
- **Data** — everything lives in `data/` (gitignored): `uploads/`, `frames/`, `thumbs/`,
  `compressed/`, and the SQLite db `app.db`. Created on startup if missing.
- **External** — OpenRouter (`https://openrouter.ai/api/v1`) for all model inference.

## Running locally

```bash
./run.sh           # creates .venv + installs deps on first run, then serves on :8000
```
Then open http://localhost:8000. Requires **`ffmpeg`** on PATH (`brew install ffmpeg`) and
**Python 3.11+**. To change the port/host, pass uvicorn args through `run.sh`.

After editing `static/app.js`, sanity-check it with `node --check static/app.js`.

## Key conventions & constraints

- **API key comes from `.env` only.** `OPENROUTER_API_KEY` is loaded at startup
  (`backend/app.py:load_dotenv`) and read from the environment. It is **never** accepted over
  HTTP or entered in the UI — do not add a UI input for it. `.env` is gitignored.
- **`ffmpeg`/`ffprobe` are hard dependencies.** Upload, thumbnails, frame extraction, and
  native-video transcoding all shell out to them. No ffmpeg → uploads/analysis break.
- **Native video IS the product.** Clips are transcoded to ~480p/10fps H.264 and sent to the
  models as **native video** input. Frame-sampling ("frames" mode) is a **legacy placeholder**
  from the first build, kept only as an optional image-model comparison — do **not** ship or
  optimize for a frame-only build.
- **Behavior uses a controlled vocabulary (ethogram)** — `llm.BEHAVIOR_CATEGORIES`. Models
  must pick one category verbatim; this is what keeps behavior labels comparable across models.
- **Re-analysis is a fresh run.** `POST /api/videos/{id}/analyze` deletes prior analyses **and
  the prior human review** for that clip (experimentation-phase behavior — see `ROADMAP.md`
  "Review persistence"). Don't assume reviews survive a re-analyze.
- **The triage workflow is the product.** Each clip has one status: `not_analyzed` →
  `confident` (models agree, auto-accepted) / `needs_review` (models disagree, the work queue)
  → `reviewed`. `compute_consensus` + the review policy decide which.
- Batch analysis is capped at `MAX_BATCH_ANALYZE = 10` clips per run.

## Configuration (in `backend/app.py`)

The **source of truth** for defaults is the code, not `README.md` (which has drifted):
- `AUTO_MODELS` — the curated model set used in "Auto" mode. Currently
  `perceptron/perceptron-mk1`, `google/gemini-2.5-flash`, `qwen/qwen3.6-plus`, `z-ai/glm-4.6v`.
- `DEFAULT_FRAME_COUNT` (16), `DEFAULT_REVIEW_POLICY` (`majority`), `DEFAULT_INPUT_MODE`
  (`video`), `MAX_BATCH_ANALYZE` (10).
- Per-session overrides (mode, selected models, frame count, review/input mode) persist in the
  `settings` table via `/api/settings`.

## API surface

`POST /api/videos` (multipart upload) · `GET /api/videos` · `GET /api/videos/{id}` ·
`POST /api/videos/{id}/analyze` · `POST /api/analyze-batch` · `POST /api/videos/{id}/review` ·
`GET /api/models` · `GET /api/scoreboard` · `GET /api/export.csv` · plus
`/api/videos/{id}/file|thumb|frames`.

## Deployment — Google Cloud Run

The app deploys **as-is** as a container to **Google Cloud Run** — chosen for always-warm
serverless + real `ffmpeg` + alignment with the future GCS pipeline. We deliberately did **not**
re-architect for a pure-serverless platform (Vercel/Cloudflare Workers) because (a) native video
needs server-side `ffmpeg`, and (b) the future direction (classify videos straight from
S3/GCS by URL) has no browser in the loop, so server-side processing is the right foundation.
Artifacts: `Dockerfile`, `.dockerignore`, `.gcloudignore`.

Decisions baked into the setup:
- **HTTP/2, not HTTP/1.** Cloud Run caps HTTP/1 request bodies at **32 MiB**, but real CCF clips
  are **40–150 MB**. Serving over **HTTP/2** removes the cap. uvicorn has no HTTP/2, so the
  container runs **hypercorn** (`backend.app:app`); deploy the service with `--use-http2`.
  (Local dev still uses uvicorn via `run.sh` — local has no size cap.)
- **Scaling:** `--min-instances=0` (scale to zero, ~$0 idle, ~2–5s cold start) for the demo;
  `--min-instances=1` for always-warm (~$40/mo). The prebuilt image makes the cold start far
  shorter than the sleep-prone free tiers we avoided.
- **In-memory filesystem.** Cloud Run's disk is RAM-backed; `data/` (uploads, frames, db) lives
  in memory, bounded by instance RAM, and **resets on redeploy/restart**. Acceptable for the
  demo. Use `--memory=4Gi` and upload in modest batches. Persistence (Neon Postgres + a GCS
  bucket/volume) is the production next step.
- **API key:** passed as the `OPENROUTER_API_KEY` env var, ideally via **Secret Manager**
  (`--set-secrets`). Never baked into the image — `.env` is excluded by `.dockerignore`. The env
  var takes precedence over `.env` (see `load_dotenv`).
- **Folder upload (shipped).** Reviewers can pick a whole folder: a `webkitdirectory` input
  (`#folderInput` in `static/index.html`) feeds the same `uploadFiles()` as the file picker /
  drag-drop. `uploadFiles()` (`static/app.js`) filters a `FileList` to video files, so non-video
  files in the folder are dropped automatically.

Deploy from GitHub (repo = deploy source):
1. `git init`, then `gh repo create` + push.
2. **Either** connect the repo in the Cloud Run console (GitHub continuous deployment — builds
   via Cloud Build from the `Dockerfile`, no local gcloud needed),
   **or** with the gcloud SDK installed:
   ```bash
   gcloud run deploy ai-for-wildlife --source . --region us-central1 \
     --use-http2 --min-instances=0 --memory=4Gi --cpu=2 --timeout=600 \
     --allow-unauthenticated \
     --set-secrets=OPENROUTER_API_KEY=openrouter-key:latest
   ```

**Future (the real goal):** clients upload straight to **GCS** via signed URLs and the app
classifies by object URL — this removes the upload-size/RAM limits and *is* the production
pipeline. See `ROADMAP.md`.

## Roadmap

`ROADMAP.md` holds the planned direction — notably the **multi-armed bandit** model selector
that will learn from human-review feedback (the scoreboard is its reward signal), task-split
models, and review persistence. `README.md` is user-facing product docs (treat code as
authoritative where they disagree).
