# Deploying to Google Cloud Run

The app runs **as-is** in a container on Cloud Run. Native-video analysis needs server-side
`ffmpeg`, and Cloud Run gives us that plus an always-warm, deploy-from-GitHub experience.

## Why these settings

- **`--use-http2`** — Cloud Run caps HTTP/1 request bodies at **32 MiB**, but real CCF clips are
  40–150 MB. Serving over HTTP/2 removes the cap. The container runs **hypercorn** (uvicorn has
  no HTTP/2); h2c is auto-negotiated. *(Validated locally: a 44.5 MB clip uploads over h2c fine.)*
- **`--min-instances=0`** — scale to zero so GCP cost is ~$0 between demos (a ~2–5s cold start
  on the first hit after idle). Set `--min-instances=1` for an always-warm instance (~$40/mo).
- **`--memory=4Gi`** — Cloud Run's filesystem is RAM-backed; uploads/frames/db live in memory.
  Give it headroom and upload in modest batches. Data **resets on redeploy** (fine for the demo;
  see "Persistence" below).
- **API key via Secret Manager** — never baked into the image (`.env` is gitignored + in
  `.dockerignore`). The env var takes precedence over any `.env`.

## One-time setup

```bash
# 1. Install + auth (interactive — run these yourself)
brew install --cask google-cloud-sdk
gcloud auth login
gcloud config set project YOUR_PROJECT_ID         # a project with billing enabled

# 2. Enable the APIs
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

# 3. Store the OpenRouter key as a secret (paste the key at the prompt, Ctrl-D to end)
gcloud secrets create openrouter-key --replication-policy=automatic
gcloud secrets versions add openrouter-key --data-file=-
```

## Deploy (run again on every change)

```bash
gcloud run deploy ai-for-wildlife \
  --source . \
  --region us-central1 \
  --use-http2 \
  --min-instances=0 \
  --memory=4Gi \
  --cpu=2 \
  --timeout=600 \
  --allow-unauthenticated \
  --set-secrets=OPENROUTER_API_KEY=openrouter-key:latest
```

`--source .` builds the `Dockerfile` with Cloud Build and deploys it. The command prints a
`https://ai-for-wildlife-….run.app` URL — share that with the CCF team.

> Prefer no terminal? In the Cloud Run console you can instead "Connect to a GitHub repository"
> for continuous deployment (builds the same `Dockerfile`), then set the same flags under the
> service's container/networking settings (HTTP/2, min instances, memory, the secret).

## Access control

`--allow-unauthenticated` makes the URL public (simplest for a demo). To restrict it, drop that
flag and grant `roles/run.invoker` to specific Google accounts, or put it behind IAP.

## Persistence (next step, not needed for the demo)

In-memory storage resets on redeploy. For durable data: move the DB to **Neon Postgres** and
store videos in a **GCS bucket** (mounted as a volume, or — the real goal — uploaded directly via
signed URLs and classified by object URL). See `ROADMAP.md` and `CLAUDE.md`.
