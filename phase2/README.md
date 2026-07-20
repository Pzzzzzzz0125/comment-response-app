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

New-file ingestion is accuracy-first and does not use the legacy city-specific OCR
or column heuristics to confirm records. For every new PDF, DOC/DOCX, XLS/XLSX,
or CSV that may contain comments or responses, it:

1. renders every page as a high-resolution image and extracts all directly readable text;
2. sends all page images and the complete raw text to Gemini for verbatim structured extraction;
3. sends all original page images and the proposed JSON through an independent Gemini verification pass;
4. confirms only records whose completeness, verbatim text, and pairing checks all pass;
5. stores every uncertain, incomplete, duplicate, conflicting, or failed-verification record as `needs_review`;
6. preserves manifests, page images, raw text, extraction JSON, verification JSON, prompt versions, and uncertainty reasons under `phase2_dataset/ingestion_artifacts/`.

Ghostscript (`gs`) is required for PDF text extraction and page rendering.
LibreOffice (`soffice`) is additionally required to render DOC, DOCX, XLS, XLSX,
and CSV documents. The importer fails explicitly when a required renderer is
unavailable; it never falls back to text-only confirmation. Large image bundles
use Gemini's temporary Files API so no page is dropped to satisfy inline request
limits.

The updater stores processed source hashes and stable source-derived IDs. Repeating
it without new sources reuses every group. A file that changes in place is rejected;
keep the immutable original and import the revision under a versioned path.

Run the complete PC3/PC4/PC5 visual regression against the 123 manually confirmed
parent pairs with:

```sh
python3 phase2/run_visual_regression.py --api-key-stdin --force
```

The regression checks PC3 = 92, PC4 = 19, PC5 = 12 and compares every comment
number, complete comment text, and paired response. A mismatch prevents the result
from becoming confirmed.

The source-registry migration is the viewer-ingestion step. It preserves originals, updates opaque document/citation IDs, and regenerates hash-addressed office previews when the corresponding original changes.

Run tests with:

```sh
python3 -m unittest discover -s phase2/tests -v
```
