# Deploying to Google Cloud Run

The app runs **as-is** in a container on Cloud Run. Native-video analysis needs server-side
`ffmpeg`, and Cloud Run gives us that plus an always-warm, deploy-from-GitHub experience.

**Live URL:** https://ai-for-wildlife.threeportkeys.com (custom domain via Cloud Run domain
mapping; the underlying `…us-central1.run.app` URL also works).

## Why these settings

- **`--use-http2`** — Cloud Run caps HTTP/1 request bodies at **32 MiB**, but real CCF clips are
  40–150 MB. Serving over HTTP/2 removes the cap. The container runs **hypercorn** (uvicorn has
  no HTTP/2); h2c is auto-negotiated. *(Validated locally: a 44.5 MB clip uploads over h2c fine.)*
- **`--min-instances=0`** — scale to zero so GCP cost is ~$0 between demos (a ~2–5s cold start
  on the first hit after idle). Set `--min-instances=1` for an always-warm instance (~$40/mo).
- **`--memory=4Gi`** — headroom for transcoding large clips; upload in modest batches.
- **Durable storage** — data is **shared and persistent** so every viewer of the link sees the
  same classified videos (survives idle/restart/redeploy):
  - **Database → Neon Postgres** via the `DATABASE_URL` secret. If unset, the app falls back to a
    local SQLite file (used for local dev only).
  - **Video files → a GCS bucket** (`ai-wildlife-500023-data`) mounted at `/app/data` (needs
    `--execution-environment=gen2`). The runtime SA has `roles/storage.objectAdmin` on it.
- **Secrets via Secret Manager** — keys never baked into the image (`.env` is gitignored + in
  `.dockerignore`). Env vars take precedence over any `.env`.

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

# 4. Durable storage:
#    a) Create a free Neon Postgres DB (https://neon.tech) and copy its connection string,
#       then store it as a secret (paste the postgresql://… URL, Ctrl-D to end):
gcloud secrets create database-url --replication-policy=automatic
gcloud secrets versions add database-url --data-file=-
#    b) Create the GCS bucket for video files and grant the runtime SA access:
gcloud storage buckets create gs://ai-wildlife-500023-data --location=us-central1 --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://ai-wildlife-500023-data \
  --member=serviceAccount:729152394086-compute@developer.gserviceaccount.com \
  --role=roles/storage.objectAdmin
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
  --execution-environment=gen2 \
  --allow-unauthenticated \
  --add-volume=name=data,type=cloud-storage,bucket=ai-wildlife-500023-data \
  --add-volume-mount=volume=data,mount-path=/app/data \
  --set-secrets=OPENROUTER_API_KEY=openrouter-key:latest,DATABASE_URL=database-url:latest
```

`--source .` builds the `Dockerfile` with Cloud Build and deploys it. The command prints a
`https://ai-for-wildlife-….run.app` URL — share that with the CCF team. **Normally you don't run
this** — merging to `main` auto-deploys via GitHub Actions.

> Prefer no terminal? In the Cloud Run console you can instead "Connect to a GitHub repository"
> for continuous deployment (builds the same `Dockerfile`), then set the same flags under the
> service's container/networking settings (HTTP/2, min instances, memory, the secret).

## Access control

`--allow-unauthenticated` makes the URL public (simplest for a demo). To restrict it, drop that
flag and grant `roles/run.invoker` to specific Google accounts, or put it behind IAP.

## Persistence (implemented)

Data is durable and shared across instances: **DB → Neon Postgres** (`DATABASE_URL` secret),
**files → GCS bucket** mounted at `/app/data`. So the link reliably shows everyone the same
classified videos. The eventual production goal is to skip manual upload and classify videos
straight from a cloud-storage URL (signed URLs / object references) — see `ROADMAP.md`.

To wipe the demo data and start fresh: clear the Neon tables and empty the bucket
(`gcloud storage rm "gs://ai-wildlife-500023-data/**"`).
