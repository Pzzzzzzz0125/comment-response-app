# Permit precedent browser

This local web application browses the source-linked Phase 2 dataset without changing extracted records. User categories are stored separately in `data/category_assignments.json`.

Start it from the workspace root:

```sh
python3 web_app/server.py
```

Then open <http://localhost:8000>. The Adobe PDF Embed client ID is registered for
`localhost`; opening the app through `127.0.0.1` uses a different origin and can
fail Adobe's domain validation.

Features:

- city summary with technical/non-technical counts and deduplicated common topics;
- city, project, round, discipline, category, and matched-status filters;
- keyword-first history search followed by bounded Gemini-assisted hybrid RAG when requested;
- display-only cleanup of spreadsheet/OCR line breaks while preserving original source text;
- selectable comments and bulk category assignment;
- historical response panel with exact source citations;
- links to the primary source plus local files and web resources referenced in the text;
- secondary-source resolution for named submission documents and explicit plan-sheet references (for example, A0.1), including page targeting and evidence-text highlighting;
- an in-app routed SourceViewer for PDF, Word previews, spreadsheets, and unsupported files;
- explicit unmatched and pending-review states.

The classification and topic grouping are deterministic exploration aids. Smart Search keeps records local for candidate retrieval. It interprets the query, creates meaning-preserving rewrites, retrieves up to 200 unique candidates, evaluates summaries in batches of 25, deeply reranks the strongest full stored records, and independently verifies the final direct/related labels. Gemini never creates citations or source locations. If Gemini is unavailable, a clearly labeled unverified deterministic fallback remains usable and may return no result.

## Smart Search index and evaluation

`data/search_index.json` stores coherent searchable units mapped to immutable historical parent comments, with city, discipline, category, code sections, acceptance signal, quality flags, content hashes, embedding model/version, and optional embeddings. Clear top-level numbered comments are split for retrieval without changing the source record, response relationship, or citation. Long historical responses are deliberately excluded from embedding input. Build Gemini embeddings incrementally (unchanged unit hashes are skipped):

```sh
python3 web_app/build_search_index.py
```

Use `--metadata-only` to refresh records without an API call. Keep the Gemini key server-side via the environment or hidden startup prompt:

```sh
python3 web_app/server.py --gemini-api-key-stdin
```

Explicit city, discipline, and category UI filters override inferred filters. Inferred metadata affects ranking but is not a hard exclusion. Development diagnostics can be enabled with `PERMIT_SEARCH_DEBUG=1`; they record pipeline/model/prompt versions and decisions without exposing filesystem paths.

Audit extraction structure, response associations, metadata, truncation, and source-file availability with:

```sh
python3 web_app/audit_search_data.py
```

Run the versioned broad evaluation fixture with:

```sh
python3 web_app/evaluate_search.py
```

The included fixture is explicitly provisional and requires domain review before it can be called a gold dataset. The command reports current retrieval metrics and the legacy token-cosine baseline. Live direct/related classification accuracy and Gemini failure rate remain unavailable until the project has API credits and the labels are reviewed.

## Gemini organization and secondary-reference analysis

Gemini enrichment is an explicit ingestion step, not a browser-time API call. It preserves the extracted dataset, stores resumable results in `data/gemini_enrichment.json`, and sends each comment/response plus same-project candidate filenames to Gemini. The result contains display-only paragraphs/lists and confidence-scored secondary sheet/document hints. The registry still verifies every hint against an authorized same-project file before exposing a source link.

```sh
GEMINI_API_KEY=your-key python3 web_app/gemini_enrich.py --workers 4
python3 web_app/migrate_sources.py
```

To prioritize responses after a partial or credit-limited run:

```sh
GEMINI_API_KEY=your-key python3 web_app/gemini_enrich.py --record-type response --workers 4
```

Results are cached by record hash, prompt version, and model, so rerunning skips completed records. The API key is read from the environment (or a hidden `--api-key-stdin` prompt) and is never written to the cache or served by the app. This processing sends permit text and candidate filenames to Google's Gemini API; use it only under the project's approved data-handling policy.

## Source registry and previews

Source citations use opaque IDs from `data/source_registry.json`; API responses never expose corpus paths. Rebuild the registry after an extraction run or whenever original documents change:

```sh
python3 web_app/migrate_sources.py
```

The migration hashes each original, normalizes citation locations, infers cited XLSX cells from the saved quote when possible, and updates previews whose original hash changed. It also resolves explicitly referenced plan sheets and named documents within the same project/submission package. Ghostscript (`gs`) is used when available to identify a referenced PDF sheet's preview page and searchable evidence phrase. DOC/DOCX previews use the replaceable `LibreOfficePreviewConverter`. Install LibreOffice and ensure `soffice` is on `PATH` to generate them. Legacy XLS files also use LibreOffice for an XLSX grid preview. XLSX and CSV viewing otherwise uses the existing Python standard-library stack and requires no SheetJS dependency.

The Adobe PDF Embed client ID can be set without editing code:

```sh
ADOBE_PDF_EMBED_CLIENT_ID=your-client-id python3 web_app/server.py
```

The supplied local client ID is the default. Adobe credentials are domain-bound; configure the registered domains in Adobe Developer Console if the viewer reports an invalid client ID. When the Adobe SDK is unavailable, PDFs fall back to the browser's inline PDF renderer.

Source APIs:

- `GET /api/sources/{source_id}` returns normalized citation and viewer routing metadata.
- `GET /api/documents/{document_id}/preview` serves PDF content inline and supports byte ranges.
- `GET /api/documents/{document_id}/spreadsheet` returns a read-only sheet window.
- `GET /api/documents/{document_id}/original` is the only attachment response.

Run tests:

```sh
python3 -m unittest discover -s web_app/tests -v
```
