# Permit corpus audit

This is a read-only discovery tool for historical permit-review files. It identifies likely city, project/scope, review round, and document type; inspects spreadsheet structure; records exact duplicates; and recommends one primary source per inferred review round.

It deliberately does not extract individual comments, match comments to responses, run OCR, call a model, create embeddings, or build the web application.

Run it from the workspace root:

```sh
python3 corpus_audit/audit_corpus.py 'comments&response' \
  --output corpus_audit_output \
  --overrides corpus_audit/manual_overrides.csv
```

Run a small deterministic dry run first:

```sh
python3 corpus_audit/audit_corpus.py 'comments&response' --output /tmp/permit-audit-subset --limit 8
```

Run the focused tests:

```sh
python3 -m unittest discover -s corpus_audit/tests -v
```

The six generated reports are always written outside the source folder. Existing report files are replaced deterministically; no source file is changed, renamed, moved, or deleted.

`manual_overrides.csv` records explicit human verification separately from deterministic inference. Overrides are cited in the inventory evidence and never alter the source corpus.
