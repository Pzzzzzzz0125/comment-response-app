# Data Ingestion Note

This note is the maintainer runbook for adding a new project folder to Permit
Precedents. The current entrance is intentionally simple: select one complete
project folder and click **Upload and ingest** once. The application performs
the internal inventory, prescan, extraction, verification, deduplication,
timeline, source-registry, and search-index stages automatically.

## Before starting

- Run the application on a trusted local workstation. Browser ingestion is
  enabled automatically only for a loopback host such as `127.0.0.1`.
- Configure the server-side `GEMINI_API_KEY` or `GOOGLE_API_KEY`. Never place an
  API key in frontend code, uploaded files, screenshots, or this repository.
- Select a complete folder for one project/site. Keep the original filenames
  and subfolder organization.
- Supported source formats are PDF, DOC, DOCX, XLS, XLSX, and CSV.
- The entrance accepts at most 5,000 supported files, 2 GB per file, and 10 GB
  for one upload session.

LibreOffice is optional for ingestion. It is needed only when a DOC/DOCX PDF
preview must be generated; native text extraction and evidence storage continue
without it.

PDF visual ingestion requires Ghostscript, and scanned PDFs require Tesseract
OCR. On Windows the application recognizes `gswin64c.exe`, `tesseract.exe`,
and `soffice.exe` in their standard installation folders. For custom install
locations, copy `.env.example` to `.env.local` and set `GHOSTSCRIPT_PATH`,
`TESSERACT_PATH`, or `LIBREOFFICE_PATH`. Temporary files use the operating
system temp directory; `/private/tmp` is no longer assumed.

## One-click workflow

1. Start the app locally:

   ```sh
   python3 web_app/server.py --host 127.0.0.1 --port 8010
   ```

   Windows PowerShell equivalent:

   ```powershell
   py -3 web_app/server.py --host 127.0.0.1 --port 8010
   ```

2. Open `http://localhost:8010`.
3. Select **Import data** in the top navigation.
4. Select the complete project folder.
5. Review the supported-file count and total size.
6. Click **Upload and ingest**.

The browser streams each supported file separately. It does not send a host
filesystem path and does not buffer the entire folder as one multipart body.
The server validates every relative path and commits the files to project
staging only after the complete upload manifest has arrived.

The dialog then reports the current stage and recent log output. Uploading must
remain open until every file has reached the server. After the upload completes,
the dialog can be closed; the server-side ingestion job continues in the
background.

## Internal stages

The one button runs these stages in order:

1. **Upload commit** — preserve original files under `new/<project>/`.
2. **Inventory** — hash files and reconcile already processed content.
3. **Prescan** — identify potentially relevant files, pages, sheets, and
   regions with a high-recall policy.
4. **Verified ingestion** — parse source structure, extract evidence, and run
   the configured pair and coverage checks.
5. **Canonicalization** — merge repeated source occurrences without deleting
   original evidence, then rebuild issue histories and recurring-issue data.
6. **Source refresh** — rebuild source/document mappings used by the in-app PDF,
   Word-preview, and spreadsheet viewers.
7. **Search refresh** — update searchable metadata without re-embedding
   unchanged text.
8. **Runtime reload** — make the completed data available to the running app.

Only one ingestion job can run at a time. File hashes, request identities, and
stage checkpoints are reused so unchanged work is not submitted to Gemini
again.

## Where data is stored

The curated processed corpus is versioned in Git so authorized coworkers can
run the application against the already-ingested records. Original source
documents, ingestion artifacts, backups, telemetry, and local job state remain
local or are distributed through the separate source bundle:

| Location | Purpose |
| --- | --- |
| `new/<project>/` | Uploaded immutable source documents and their original folder structure |
| `phase2_dataset/dataset.json` | Authoritative extracted comments, responses, source occurrences, canonical events, relationships, dates/rounds, and issue-history data |
| `phase2_dataset/comments.csv` | Comment review/export projection; not an independent source of truth |
| `phase2_dataset/responses.csv` | Response review/export projection; not an independent source of truth |
| `phase2_dataset/comment_response_links.csv` | Relationship review/export projection |
| `phase2_dataset/pipeline_checkpoint.json` | Resumable pipeline stage and version state |
| `phase2_dataset/ingestion_admin_jobs.json` | Recent browser-triggered ingestion jobs |
| `phase2_dataset/ingestion_jobs/` | Per-job maintainer logs |
| `phase2_dataset/ingestion_artifacts/` | Parser, evidence-packet, verification, and telemetry artifacts when generated |
| `web_app/data/source_registry.json` | Runtime citation and source-viewer mapping |
| `web_app/data/search_index.json` | Runtime search projection |

Do not commit `new/`, `comments&response/`, API credentials, original permit
documents, `phase2_dataset/ingestion_artifacts/`, `dataset.pre-*` backups, or
local ingestion job logs. Only the exact processed-data files allowlisted in
`.gitignore` belong in Git.

## Verification after completion

After the job reports **complete**:

1. Refresh the selected city and confirm the new project count appears.
2. Open several new comments and responses in the Library.
3. Open PDF, spreadsheet, and Word-preview citations and verify the page, cell,
   paragraph, and highlight location.
4. Confirm repeated appearances are represented as one canonical event with
   multiple source links rather than multiple timeline events.
5. Check that event dates, effective rounds, observed document rounds, and
   response roles match the source.
6. Run representative Knowledge Chat questions and confirm every answer uses
   relevant supporting evidence.

An ingestion run finishing successfully means the automated pipeline completed;
it does not replace spot-checking source accuracy for a newly encountered
document template.

## Failure and recovery

- **Upload fails:** choose the folder again and rerun. A failed upload is never
  treated as a completed project intake.
- **Gemini is not configured:** configure the server-side key and restart the
  app. The frontend never receives the key.
- **A job fails after upload:** inspect the log shown in **Import data** or the
  corresponding file under `phase2_dataset/ingestion_jobs/`, fix the cause, and
  rerun. Existing hashes and completed checkpoints are reused.
- **The server restarts during a job:** the job is marked `interrupted`; source
  files and pipeline checkpoints remain available for a new run.
- **An Office preview is unavailable:** install LibreOffice if the preview is
  required. Do not treat missing preview conversion as missing extracted text.

For automated recovery or diagnosis, the CLI remains available:

```sh
python3 phase2/incremental_update.py --help
```

The browser entrance is the preferred routine workflow; the CLI is for
maintenance, debugging, and checkpoint recovery.
