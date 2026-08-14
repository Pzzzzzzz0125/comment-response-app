# Phase 2: source-linked comment/response dataset

Phase 2 processes the primary source selected for every audited review round. It retains comment-only records as explicit unmatched links rather than dropping them or inventing responses.

From the workspace root:

```sh
python3 corpus_audit/audit_corpus.py 'comments&response' \
  --output corpus_audit_output \
  --overrides corpus_audit/manual_overrides.csv

python3 phase2/extract_dataset.py \
  --audit-dir corpus_audit_output \
  --output phase2_dataset \
  --review-decisions phase2/review_decisions.csv

python3 web_app/migrate_sources.py
```

The verified four-page plan-review PDF uses targeted local OCR through the installed Ghostscript and Tesseract commands. No other PDF is OCRed, and no model or paid service is called.

`review_decisions.csv` records human confirmation separately from extracted source data. Confirmed unmatched comments remain unmatched; the decision never creates a response that does not exist.

## Incremental updates

When new city/project folders are added, reuse the existing audit for unchanged files and force inspection only for the new path prefixes:

```sh
python3 corpus_audit/audit_corpus.py 'comments&response' \
  --output corpus_audit_output \
  --overrides corpus_audit/manual_overrides.csv \
  --reuse-inventory corpus_audit_output/file_inventory.json \
  --reprocess-prefix 'comments&response/NEW_FOLDER/'
```

Then append only newly selected review-round sources:

```sh
python3 phase2/incremental_update.py \
  --audit-dir corpus_audit_output \
  --output phase2_dataset \
  --review-decisions phase2/review_decisions.csv \
  --gemini-api-key-stdin

python3 web_app/migrate_sources.py
```

New-file ingestion is content-driven and accuracy-first. Filenames only affect
priority. Every supported file is registered and receives local completeness
coverage:

1. inventory every PDF, DOC/DOCX, XLS/XLSX, and CSV with its SHA-256 and folder metadata;
2. collapse identical SHA-256 and normalized-content copies to one canonical source before Gemini;
3. parse native structure; known XLSX/CSV schemas become exclusive row/cell evidence units without office rendering;
4. for PDFs and visually ambiguous office files, render 120-DPI screening images for every page and OCR only sparse pages;
5. save a completeness manifest, then either send one compact spreadsheet unit-verification request or render selected PDF/office evidence pages at 220 DPI for Gemini extraction;
6. independently verify every proposed record, escalating only uncertain PDF pages to 300/360 DPI and OCR comparison;
7. reconcile visible numbering, response labels, page signals, processed pages, and verified records deterministically;
8. match comments and responses only after verification, using an explicit shared printed identifier;
9. index only verified records and retain uncertain or incomplete results as `needs_review`.

Model roles are intentionally separate:

- document extraction and second-pass verification use
  `INGESTION_GEMINI_MODEL` / `--gemini-model` (default
  `gemini-3.6-flash`);
- optional Gemini prescan and simple file classification use
  `PRESCAN_GEMINI_MODEL` / `--prescan-gemini-model` (default
  `gemini-3.1-flash-lite`);
- normal full ingestion uses deterministic local prescan, so the lightweight
  prescan model is called only by an explicit `--prescan-only` run.

City resolution is also content-first. The inventory checks, in order:
authoritative city domains/emails and permit portals, an explicit postal address
or `City of ...` letterhead, municipal-code references, and finally folder/file
aliases. A high-confidence result from one source is propagated to the other
files in the same physical site folder. Only a site with no reliable office-file
or path signal triggers a bounded PDF probe; unresolved or conflicting sites
remain `Unknown` instead of being guessed.

PDFs use native page text plus gated OCR. DOCX preserves paragraph/table indexes,
styles, comments, and headings. XLSX preserves exact source values separately
from display/cached values and formulas, plus sheet names, row numbers, cell
addresses, hidden state, cell comments, and likely comment/response columns.
Known ProjectDox and explicit comment/response tables are extracted locally:
each physical row is one exclusive evidence group, comments and responses may
only pair within that row, and Gemini receives the compact groups once to verify
IDs and relationships without retranscribing text. Unknown or structurally
ambiguous workbooks remain `needs_review` or take the visual fallback; their
complete cell payload is never repeated across preview-page batches. CSV uses
the same row/cell evidence model. Before transmission, the Gemini copy of DOCX
structure drops empty paragraphs and empty/default metadata fields while
retaining every non-empty source character, paragraph/table index, style,
heading, and Word comment. The raw audit artifact remains unchanged.

The hash-addressed cache stores each stage independently: native text, OCR,
screening thumbnails, page manifests, selected high-resolution pages, Gemini
extraction, Gemini verification, matching, and job progress under
`phase2_dataset/ingestion_artifacts/`. Changing a verification prompt reruns
verification only; it does not invalidate native extraction, OCR, rendering, or
Gemini extraction. Each artifact audit includes completeness counts, unresolved
signals, pages screened/processed/escalated, wall time, Gemini call counts,
per-request bytes/tokens/attempts/model/timing, cached and thought tokens, and
cache-hit percentage.

Each processed document also writes an append-only `straggler_trace.jsonl` and
a compact `straggler_summary.json` in its artifact directory. The trace records
request creation, uploads, submission attempts, first/last streamed output,
token usage, image/evidence counts, expected versus actual records, response
bytes, retries, finish reason, and terminal status. Summaries report overall and
per-stage p50/p95 latency plus likely TTFT, generation, retry, and unknown-status
stragglers. Gemini does not expose an exact server queue duration in this API,
so `queue_duration` remains null and `pre_generation_wait`/TTFT is reported as
the measurable client-side wait instead of presenting an estimate as fact.
Known high-demand `429`/`503` responses receive at most one bounded retry; the
completed page checkpoints remain available for a later resume rather than
holding the entire site open through repeated service-capacity waits.

Visual PDF batches use two isolated Gemini clients by default. Non-overlapping
batches run concurrently; adjacent overlap batches run in separate waves so
cross-page context is preserved without simultaneous work on the same page.
Set `--visual-batch-workers 1` for strictly sequential processing or override
the default with `VISUAL_BATCH_WORKERS`. Verification requests retain record
IDs, exact source text, cited pages, and bounding boxes, while dropping
normalized text and extraction-only metadata that the image audit does not
need.

Create or refresh the complete inventory without calling Gemini:

```sh
python3 phase2/incremental_update.py --inventory-only
```

When Gemini is temporarily unavailable, exact XLSX cells can be staged
idempotently for review without spending model tokens:

```sh
python3 phase2/incremental_update.py --offline-structured \
  --site NEW_SITE_A --site NEW_SITE_B
```

These rows retain sheet/row/cell citations but are quarantined as
`needs_review`, excluded from production search, and deliberately left
unprocessed so a later successful visual two-pass run can replace them.

After upgrading the spreadsheet pipeline, selectively replace only older
Gemini-visual XLSX/CSV rows with the deterministic cell-unit route:

```sh
python3 phase2/incremental_update.py \
  --refresh-structured-spreadsheets \
  --site NEW_SITE_A --site NEW_SITE_B
```

This selector intentionally excludes manually confirmed/rematched spreadsheet
links. A Gemini spending-cap or verification failure retains the newly parsed
rows as `needs_review`; rerunning the same command retries only the replaceable
structured sources.

Benchmark new site folders locally without calling Gemini:

```sh
python3 phase2/benchmark_site_intake.py \
  'comments&response/NEW_SITE_A' \
  'comments&response/NEW_SITE_B' \
  --output phase2_dataset/site_preflight_report.json
```

The preflight uses local file hashes, direct text, spreadsheet rows, and PDF
page signals only. It reports which files are `visual_full_read`,
`structured_spreadsheet`, or `context_only`, together with selected evidence
pages/rows, low-central-high input-token and elapsed-time estimates, and a
`requires_confirmation` flag for expensive files or unresolved cities. It does
not call Gemini or spend API quota. Estimates are planning ranges rather than
promises because Gemini demand, OCR escalation, and uncertain layouts can
change the actual run.

By default the preflight reads `phase2_dataset/dataset.json` and estimates only
new files or sources whose hash changed. Unchanged files and identical-hash
aliases are reported as `cache_reuse` with zero Gemini cost. Add
`--ignore-dataset-cache` only when estimating a completely fresh ingestion.
Its default visual configuration matches production: six pages per batch, one
overlap page, and two batch workers.

Full ingestion also uses two isolated file workers by default. Each file owns
its pipeline metrics and Gemini client; a slow source therefore does not stop a
different source from being extracted and checkpointed. Combined with the two
page-batch workers, the default upper bound is four concurrent Gemini requests.
Use `--folder-workers 1` when a quota requires serial source processing.

Run a bounded Gemini routing benchmark for only those folders:

```sh
python3 phase2/incremental_update.py --prescan-only \
  --prescan-site NEW_SITE_A --prescan-site NEW_SITE_B \
  --prescan-workers 3
```

Prescan requests are coalesced per physical site (up to 20 files per request)
even when the audit metadata splits that site into multiple rounds or package
subfolders. Independent sites are routed concurrently; `--prescan-workers 1`
keeps strictly sequential behavior when required by an API quota.

The per-file report is written to `phase2_dataset/ingestion_report.json`. A
terminal run must satisfy
`discovered_files = processed_files + cached_files + failed_files`; an
inventory-only run additionally reports files that are still `pending`.

Ghostscript (`gs`) and Tesseract (`tesseract`) are required for PDF screening,
OCR, and selected-page rendering. Known XLSX and CSV comment tables do not
require LibreOffice. LibreOffice (`soffice`) remains required for DOC/DOCX/XLS
previews and visually ambiguous spreadsheet fallback. A missing dependency is
reported per file as `failed`; the importer never silently confirms an
ambiguous text-only extraction. Large selected-image bundles use Gemini's
temporary Files API.
Optional PyMuPDF (`fitz`) enables exact per-page annotation, widget, and drawing
counts. Without it, a PDF containing `/Annots` or `/AcroForm` markers is
conservatively routed for full-document Gemini analysis instead of being skipped.

The updater stores processed source hashes and stable source-derived IDs. Repeating
it without new hashes reuses cached artifacts. A changed hash at the same path is
selectively reprocessed; replaced rows are retained in `repair_history` for audit.

### Prescan and selective repair

To triage an already audited corpus without extracting every supporting report:

```sh
python3 phase2/incremental_update.py \
  --prescan-only --prescan-include-processed --gemini-api-key-stdin
```

The plan is saved as `phase2_dataset/prescan_plan.json`. Menlo Park rows can then
be repaired from only its `full_read` files. Existing rows are archived in
`repair_history` and in a timestamped `dataset.pre_prescan_repair-*.json` backup;
the write occurs only after dataset validation succeeds:

```sh
python3 phase2/incremental_update.py \
  --repair-prescan --repair-city 'Menlo Park' --gemini-api-key-stdin
```

The repair is resumable. Use `--repair-source 'PC3-'` (repeatable) to process one
source family at a time. The default extraction render is 220 DPI; screening stays
at 120 DPI. `--repair-force` regenerates cached Gemini extraction and verification
artifacts while stage caches retain independently reusable local evidence.

Run the complete PC3/PC4/PC5 visual regression against the 123 manually confirmed
parent pairs with:

```sh
python3 phase2/run_visual_regression.py --api-key-stdin --force
```

The regression checks PC3 = 92, PC4 = 19, PC5 = 12 and compares every printed
comment ID and paired response. It deliberately does not use the legacy government-
comment field as an oracle because that is the field being repaired. A count, ID,
response, verification, or locator mismatch prevents the result from becoming
confirmed.

Apply the verified repair, the 67 exact DOCX paragraph-locator corrections, and
the 61-record legacy orphan-response quarantine with:

```sh
python3 phase2/repair_verified_dataset.py --api-key-stdin --force-gemini --apply
python3 web_app/migrate_sources.py
python3 web_app/build_search_index.py --metadata-only
```

The repair is atomic for the 123 Warner parent links: all records must independently
pass the two visual checks and agree with the existing confirmed response, otherwise
none of those parent comments is changed. `original_text` remains immutable audit
data; verified text is stored separately in `verified_text`. Search, Knowledge Chat,
city summaries, and comment display use the verified value and exclude quarantined
records. `--local-only --apply` safely performs only the DOCX locator correction and
legacy orphan quarantine when external Gemini processing is unavailable.

The source-registry migration is the viewer-ingestion step. It preserves originals, updates opaque document/citation IDs, and regenerates hash-addressed office previews when the corresponding original changes.

### Versioned evidence model and checkpoints

Every incremental or selective-repair write also publishes a deterministic
`phase2_dataset/evidence_model.json` sidecar. The legacy `dataset.json` rows are
left intact for compatibility; the sidecar is the normalized projection:

```text
raw document → raw extraction → evidence packet → source occurrence
→ canonical event → issue timeline → common topic → verified index
```

`source_occurrences` retain the exact quote, source hash, page/row/cell or
paragraph locator, and date/round provenance. A `canonical_event` groups one
same-date/same-round event while retaining every supporting source occurrence.
The sidecar never uses confidence alone to mark a record confirmed. Its gate
requires original text, a locator, verbatim verification, pair verification,
coverage verification, no conflict, and an explicit date or round. Records
missing one of these checks are marked `needs_review` in the projection and are
not eligible for verified search.

Each raw document also has a versioned checkpoint for `uploaded`, `parsed`,
`prescanned`, `extracted`, `verified`, `deduplicated`, `timeline_linked`, and
`indexed`, including parser, prescan, extraction, verification, and dedup
versions. Re-running a later stage therefore does not require re-reading or
re-sending earlier stages to Gemini. The sidecar can be rebuilt locally with
`phase2.evidence_model.materialize_evidence_model` without any API call.

### Verbatim reconstruction and non-regression migration

The additive representation layer keeps one authoritative readable wording
without replacing source evidence. Comment and response records may carry:

```text
text_raw                    immutable parser/OCR/cell text
text_reconstructed          lexical-preserving readable text
display_structure           paragraph/list/heading offsets for the UI
normalized_identity_text_v2 candidate identity normalization
normalized_search_text_v2   search/embedding normalization
source_unit_ids             PDF span/region, spreadsheet cell/row/range,
                            DOCX paragraph/table cell, or CSV cell/row IDs
reconstruction              version, method, verification, and uncertainty
```

Reconstruction may remove forced line breaks and proven export noise such as
`_x000d_`, but it cannot paraphrase, correct wording, change numbers/codes, or
merge comment and response text. `display_structure` is presentation-only and
never participates in canonical identity, pairing, timeline ordering, search
identity, or topic membership. Search and display prefer verified reconstructed
text and fall back to legacy verified/original text during migration; source
navigation continues to use the original locator.

For new documents, verification may report a layout-only reconstruction defect.
The pipeline permits one bounded correction request in that case. A candidate
is accepted only when its lexical token and punctuation signature is identical
to the exact source text, followed by an independent verification pass. Missing
records, incorrect pairings, dates, or locators never enter this correction
path and remain `needs_review`.

Existing data uses an idempotent, deterministic, additive backfill. It does not
call Gemini, merge records, or rebuild timelines:

```sh
python3 phase2/reconstruct_existing.py \
  --dataset phase2_dataset/dataset.json \
  --output phase2_dataset/dataset.reconstructed.json
```

The default output is separate from the active dataset. The command writes a
report, checkpoint, and `reconstruction-non-regression-snapshot-v1` containing
complete before/after relationship maps (canonical IDs, issue IDs, links,
dates, rounds, source occurrence IDs/locators, and ordered timeline arrays).
It fails rather than accepting a changed relationship graph. Use `--in-place`
only after reviewing the report and snapshot. A later duplicate audit may
produce candidates, but canonical repair remains a separate explicit operation.

### Cross-file issue timelines

Later review files can repeat earlier issue events before appending a new
follow-up. Build one source-preserving timeline across every city with:

```sh
python3 phase2/repair_issue_event_duplicates.py
python3 phase2/repair_issue_event_duplicates.py --apply
python3 web_app/migrate_sources.py
```

The dry run reports affected threads and repeated event occurrences. The
applied repair groups only compatible records from the same site and printed
comment number, splits explicit `PC1` / `PC2` / later-round progressions, and
stores each unique event once in `issue_event_index`. If an event appears in
multiple files, every file and locator is retained in `source_occurrences` and
shown as an additional citation. Original and verified extraction text is not
rewritten. City common-topic counts treat all snapshots in one issue timeline
as one logical occurrence.

Run tests with:

```sh
python3 -m unittest discover -s phase2/tests -v
```
