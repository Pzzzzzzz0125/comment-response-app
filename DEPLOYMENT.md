# Deployment guide

## Recommended production shape

Deploy the application as two services:

```text
Browser
  ├── Vercel: React/Vite frontend
  └── Persistent Python API
        ├── mounted private runtime data
        ├── mounted authorized source documents
        └── Gemini API
```

The current production dataset and source corpus must **not** be copied into a
Vercel Function or a public frontend deployment. Vercel is the static frontend
host. The Python API must run on a long-lived service with a persistent disk,
or be migrated to a database plus object storage before broad production use.

This separation is required because the app serves large source documents,
supports PDF byte-range requests, updates review/runtime records, and runs
long ingestion jobs. Vercel Functions also enforce request/response and bundle
limits that are not suitable for the private source corpus.

## 1. Deploy the persistent Python API

The root `Dockerfile` starts `web_app/server.py` and accepts either `PORT` or
`PERMIT_PORT`. It is suitable for a persistent container host such as Render,
Railway, Fly.io, Cloud Run with external persistent storage, or a managed VM.

Mount the authorized runtime data and set these paths:

| Variable | Persistent content |
| --- | --- |
| `PERMIT_DATASET_PATH` | canonical `dataset.json` |
| `PERMIT_CATEGORIES_PATH` | category/tag assignments |
| `PERMIT_SOURCE_ROOT` | original authorized source files |
| `PERMIT_SOURCE_REGISTRY_PATH` | opaque source/document registry |
| `PERMIT_PREVIEW_ROOT` | generated PDF previews |
| `PERMIT_ENRICHMENT_PATH` | verified enrichment output |
| `PERMIT_SEARCH_INDEX_PATH` | generated search index |
| `PERMIT_LINK_REVIEWS_PATH` | link review decisions |
| `PERMIT_WORKBOOK_REVIEWS_PATH` | workbook review decisions |

Required/recommended backend settings:

```dotenv
PERMIT_HOST=0.0.0.0
PORT=8000
PERMIT_ALLOWED_ORIGINS=https://permit.example.com,https://your-app.vercel.app
GEMINI_API_KEY=replace-in-host-secret-manager
ADOBE_PDF_EMBED_CLIENT_ID=production-domain-client-id
PERMIT_SKIP_SOURCE_REGISTRY_MIGRATION=true
```

`PERMIT_ALLOWED_ORIGINS` is an exact comma-separated allowlist. Do not use a
wildcard for private permit data. Include the final custom frontend domain and
any explicitly authorized Vercel preview domain.

Verify the deployed service:

```sh
curl https://permit-api.example.com/api/health
curl -I -H 'Origin: https://permit.example.com' \
  https://permit-api.example.com/api/documents/DOCUMENT_ID/preview
```

The second request should return the exact allowed origin and PDF range-related
headers. Test an actual `Range: bytes=0-1023` request before launch.

Browser ingestion is intentionally disabled when the API is bound publicly.
Only add `--enable-ingestion-admin` after production authentication, upload
limits, malware scanning, and a durable background-job runner are present.

## 2. Deploy the Vite frontend to Vercel

Create a Vercel project from this repository with:

```text
Root Directory: frontend
Framework: Vite
Build Command: npm run build
Output Directory: dist
```

Set this Vercel environment variable for Preview and Production:

```dotenv
VITE_API_BASE_URL=https://permit-api.example.com
```

`VITE_API_BASE_URL` is embedded into the browser build and is therefore public.
It must contain only the backend origin, never a secret or API key. Environment
changes apply only to new Vercel deployments, so redeploy after changing it.

The frontend's `vercel.json` supplies SPA routing and basic response headers.
The Vite config continues to build into `web_app/static` locally, but builds to
`frontend/dist` when Vercel sets `VERCEL=1`.

Register both the final frontend domain and any needed preview domain in the
Adobe PDF Embed project. A client ID authorized only for `localhost` will not
work on the deployed domain.

## 3. Security gate before real permit data

CORS is not authentication. Do not expose the production dataset simply because
the frontend and backend can communicate.

Before serving real permit data to coworkers, place organization authentication
and per-document authorization in front of **every** `/api/*` endpoint. A safe
first deployment may use the synthetic demo dataset. A real deployment should
use an identity-aware reverse proxy or application sessions, and the backend
must continue validating access when serving each source/document ID.

Also configure:

- TLS-only custom domains;
- secret-manager values rather than committed `.env` files;
- encrypted persistent disks/object storage;
- backups for review decisions and canonical data;
- access logs that do not include extracted permit text or API keys;
- request-size and rate limits;
- a private background worker for ingestion.

## 4. Production smoke test

Run these checks in order:

1. Frontend loads and `/api/health` succeeds through the configured API origin.
2. City summary and Library records load.
3. Keyword and Smart Search return the expected scoped records.
4. AI Chat answers from confirmed evidence and citations open the correct record.
5. PDF opens inline, navigates to the cited page, highlights evidence, and
   supports range requests.
6. XLS/XLSX opens the cited sheet and highlighted range.
7. DOC/DOCX preview state is explicit; missing previews never trigger a silent
   download.
8. A disallowed browser origin receives no CORS authorization.
9. Original filesystem paths and private filenames are not exposed as paths.
10. Ingestion controls are unavailable on the public API unless explicitly and
    securely enabled.

## 5. Data migration path

The least disruptive first deployment is one persistent backend volume using
the current JSON/runtime files. The scalable target is:

```text
PostgreSQL
  canonical events, timelines, tags, review decisions, source registry metadata

Object storage
  immutable originals, generated previews, extracted evidence artifacts

Background worker
  ingestion, Gemini verification, indexing, preview conversion
```

Keep opaque `document_id`, `source_id`, canonical event IDs, provenance, and
locators stable during that migration so citations and timeline relationships
do not change.

Official references:

- [Vite on Vercel](https://vercel.com/docs/frameworks/frontend/vite)
- [Vercel environment variables](https://vercel.com/docs/environment-variables)
- [Vercel Functions limits](https://vercel.com/docs/functions/limitations)
