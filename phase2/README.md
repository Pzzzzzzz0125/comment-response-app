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
  --review-decisions phase2/review_decisions.csv

python3 web_app/migrate_sources.py
```

The updater stores processed source paths and stable source-derived IDs. Repeating it without new sources reuses every group and produces byte-identical datasets.

The source-registry migration is the viewer-ingestion step. It preserves originals, updates opaque document/citation IDs, and regenerates hash-addressed office previews when the corresponding original changes.

Run tests with:

```sh
python3 -m unittest discover -s phase2/tests -v
```
