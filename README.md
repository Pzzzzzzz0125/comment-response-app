# Comment-response app

This public repository contains the permit-comment browser, extraction/audit tools,
tests, and a small synthetic dataset for review. Real permit documents and derived
production data are intentionally excluded.

Run the synthetic demo from the repository root:

```sh
python3 web_app/server.py \
  --dataset demo_data/dataset.json \
  --source-root demo_sources \
  --categories demo_data/category_assignments.json \
  --source-registry demo_data/source_registry.json \
  --preview-root demo_data/previews \
  --enrichment demo_data/gemini_enrichment.json \
  --search-index demo_data/search_index.json \
  --link-reviews demo_data/link_review_decisions.json
```

Open <http://localhost:8000>. Smart Search uses its clearly labeled deterministic
fallback unless a Gemini key is supplied with `--gemini-api-key-stdin`.

Run all tests:

```sh
python3 -m unittest discover -s web_app/tests
python3 -m unittest discover -s phase2/tests
python3 -m unittest discover -s corpus_audit/tests
```

See [`web_app/README.md`](web_app/README.md) for architecture, ingestion, viewer,
Gemini, and evaluation details.
