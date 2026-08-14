# New-folder ingestion report — 2026-08-13

## Scope correction

The `new` folder contains older projects that were already ingested. This run was therefore restricted to the actual new batch only:

```text
25-023 through 25-031
```

Projects `25-001` through `25-021` were excluded from the ingestion scope. The corrected batch contains **9 new project folders**, not the entire `new` directory.

## Result at a glance

| Metric | Result |
|---|---:|
| New folders discovered / projects identified | 9 |
| Supported files accounted for | 805 |
| Files opened for content ingestion | 73 |
| Files classified locally without full ingestion | 732 |
| Files producing comments or responses | 66 |
| Opened files with no relevant permit-history content | 6 |
| Initial existing SHA-256 hits | 9 |
| Completed files reused by the checkpoint retry | 797 |
| New government comments | 586 |
| New applicant responses | 128 |
| Canonical reviewer follow-ups | 30 |
| Confirmed comment-response links | 114 |
| Needs-review comment-response links | 14 |
| Source occurrences added | 714 |
| Canonical events after occurrence-level consolidation | 564 |
| Search-eligible canonical events | 420 |
| Canonical events retained as `needs_review` | 144 |
| Issue timelines built or extended by this batch | 507 |
| Canonical events supported by multiple new source occurrences | 143 |
| Repeated raw event occurrences collapsed automatically | 126 |
| Suspected duplicate groups still requiring review | 0 |
| Needs-review comments and responses | 164 |
| Failed/unreadable files | 1 |

The difference between **714 source occurrences** and **564 canonical events** is intentional. When the same event appears in more than one file, the app retains every citation but displays one historical event. It does not count copied history as new activity.

## How the batch was processed

1. The nine project trees were inventoried locally. File type, size, path, project hints, and SHA-256 were collected without Gemini.
2. Existing hashes and ingestion checkpoints were checked before content work. Nine files had an existing hash at first comparison.
3. Local prescan and deterministic routing accounted for all 805 supported files. Only 73 files were opened for content extraction; 732 were handled as local classifications rather than being sent for full visual analysis.
4. Native parsers preserved Excel cells, PDF pages/locators, and DOCX structure. Gemini 3.6 Flash was used only for routed visual extraction, role separation, complex tables, and verification.
5. Extraction used the existing pair-verification and coverage-verification contracts. Uncertain records stayed out of formal search.
6. Repeated source appearances were consolidated into canonical events before timeline construction. All source occurrences were retained.
7. Search metadata was refreshed locally; no embedding regeneration API call was made for unchanged records.
8. The source registry was rebuilt without Ghostscript full-document secondary-reference discovery. Existing page, exact-quote, and spreadsheet locators were preserved while avoiding a multi-hour full-corpus PDF scan.

## Data and source quality

- **114 links** passed confirmed matching and verification.
- **14 matched links** remain `needs_review`; they were not silently promoted to confirmed.
- **322 comments** are verified historical comments with no matched response. Another **136 unmatched comments** remain `needs_review`.
- Every one of the 586 comments and 128 responses has a stored source locator.
- All 714 comment/response owners have at least one opaque source citation in the web source registry.
- The new source registry exposes 1,641 citations across 138 referenced documents.
- All 512 spreadsheet citations have a sheet and cell range.
- PDF citations have a cited page and exact quote, but this batch did not persist PDF bounding boxes into the web registry. PDF navigation therefore uses cited page plus exact-text search rather than claiming coordinate highlighting.
- Canonical date provenance is `document_body` for 296 events, `document_header` for 213, `response_text` for 2, and unknown for 53. Unknown dates are not invented.
- Nineteen extracted comments have an unknown effective round. Most other round values come from document content; filename hints do not override explicit document evidence.
- `Los Altos` also appears as `LOS ALTOS` in six rows. This is a casing-normalization issue, not a separate city, and should be normalized in a future data-only repair.

## Project-by-project summary

### 25-023 — 531 Easter Avenue, Milpitas

- **Files accounted for:** 4
- **History found:** 0 comments, 0 responses
- **Confirmed links:** 0
- **Result:** All four files were classified locally. No comment/response evidence was found, so the pipeline did not manufacture empty historical records.

### 25-024 — 117 Scharff Avenue, San Jose

- **Files accounted for:** 121
- **Rounds found:** 1–4
- **Records:** 179 comments, 7 responses
- **Confirmed links:** 7
- **Confirmed/searchable comments:** 152
- **Needs-review comments:** 4
- **Issue history:** 151 timelines, 186 canonical timeline events, 17 reviewer follow-ups, 17 timeline response events
- **Main review areas:** building review, improvement-plan review, grading/drainage, fire, zoning, and Public Works conformance
- **Recorded actions include:** special-inspection requirements, exterior-wall/eave revisions, driveway coordination, equipment relocation, and truss-review documentation
- **Important limitation:** one 38-page Planning Review PDF failed with Gemini HTTP 503 and remains unextracted. This project is therefore not fully complete.

### 25-025 — Camden Avenue multi-address project, San Jose

- **Files accounted for:** 24
- **Round found:** 1
- **Records:** 6 comments, 0 responses
- **Confirmed links:** 0
- **Confirmed/searchable comments:** 6
- **Main review area:** General Plan and planning-policy review
- **Timeline note:** six independent comment histories; no applicant response evidence was present in the ingested sources.

### 25-026 — 2398 Arden Way, San Jose

- **Files accounted for:** 19
- **History found:** 0 comments, 0 responses
- **Confirmed links:** 0
- **Result:** The files were handled locally and did not yield review-comment or response history.

### 25-027 — 2130 Dry Creek Road, San Jose

- **Files accounted for:** 35
- **Round found:** 1
- **Records:** 17 comments, 0 responses
- **Confirmed links:** 0
- **Confirmed/searchable comments:** 17
- **Main review areas:** building review, planning permit conformance, Public Works conformance, and fire
- **Representative requirements:** submission pathway clarification, site-plan material, fence-height callouts, and building-plan revisions
- **Timeline note:** no response or later follow-up evidence was present in this batch.

### 25-028 — 1019 North San Antonio Road, Los Altos

- **Files accounted for:** 189
- **Rounds found:** round 1 plus 18 records with unknown round
- **Records:** 110 comments, 23 responses
- **Confirmed links:** 19; another 4 matched relationships remain `needs_review`
- **Searchable comments:** 26; **needs-review comments:** 66
- **Main review areas:** fire department, accessibility, architecture, structural, mechanical, plumbing, and third-party building review
- **Recorded actions include:** adding professional stamps, updating water-heater details, identifying equipment, relocating grease-interceptor equipment, and sheet-specific revisions
- **Quality note:** this project contains the largest concentration of uncertain comment records in the batch. They remain outside formal search pending review.

### 25-029 — 4155 Mitzi Drive, San Jose

- **Files accounted for:** 275
- **Rounds found:** 1–2, plus one unknown-round record
- **Records:** 106 comments, 13 responses
- **Confirmed links:** 13
- **Searchable comments:** 67; **needs-review comments:** 11
- **Issue history:** 90 timelines, 109 canonical events, 9 reviewer follow-ups, 10 timeline response events
- **Main review areas:** City Surveyor map review, building, grading/drainage, zoning, planning, and Public Works
- **Recorded actions include:** grading-permit coordination, plan-sheet corrections, easement verification, tree-removal documentation, map revisions, and response-letter revisions
- **Timeline note:** sixteen copied event occurrences were collapsed into their existing canonical events rather than displayed as new history.

### 25-030 — 718 Belden Drive, Los Altos

- **Files accounted for:** 78
- **Rounds found:** 1–2
- **Records:** 82 comments, 16 responses
- **Confirmed links:** 6; another 10 matched relationships remain `needs_review`
- **Searchable comments:** 15; **needs-review comments:** 67
- **Main review areas:** planning, fire department, and fire prevention
- **Recorded response claims include:** site-plan changes, setback dimensions, tree information, elevation/material updates, and landscape calculations
- **Quality note:** ten responses and most comments remain `needs_review`; the app does not present these as confirmed handling evidence.

### 25-031 — 7298 Queensbridge Way, San Jose

- **Files accounted for:** 60
- **Rounds found:** 1–2
- **Records:** 86 comments, 69 responses
- **Confirmed links:** 69
- **Searchable comments:** 18; **needs-review comments:** 2
- **Issue history:** 71 timelines, 82 canonical events, 4 reviewer follow-ups, 7 timeline response events
- **Main review areas:** building, planning, Public Works, zoning, geologic-hazard review, grading, and fire
- **Recorded actions include:** separate grading and geologic-hazard submissions, cover-sheet changes, plan references, and permit cancellation/new-application coordination
- **Timeline note:** 71 repeated event occurrences were consolidated. This is a strong example of copied history being retained as multiple sources without inflating the number of events.

## Important historical findings

- Across these nine projects, the largest confirmed bodies of new history concern building review, fire review, planning, grading/drainage, map review, and Public Works coordination.
- The records contain many document-specific responses such as revised sheets, updated calculations, separate permit applications, stamped sheets, equipment relocation, and supporting reports. These are more useful than generic “noted” responses because they identify a concrete action or source.
- Scharff Avenue, Mitzi Drive, and Queensbridge Way contain meaningful multi-event issue histories. Their reviewer follow-ups remain distinct from applicant responses and are ordered as canonical timeline events.
- Repeated workbooks and carried-forward histories were common. The ingestion model retained 714 source occurrences while reducing them to 564 events; 126 raw event repetitions were collapsed by the issue-event layer.
- The batch does not demonstrate that every response resolved its comment. Applicant statements remain separate from reviewer confirmation, and unresolved or uncertain records remain labeled accordingly.

## Automatically resolved data issues

- Previously ingested projects in `new` were excluded after scope reconciliation.
- Checkpoint retries reopened only failed hashes; successfully completed files were not resubmitted.
- Copied historical events were attached as additional source occurrences rather than new events.
- Exact source locators were retained for all extracted records.
- Search metadata was rebuilt without re-embedding unchanged comments.

## Ingestion-time review queue

1. **Failed source:** `new/25-024-117 Scharff Avenue, San Jose, CA/07 Deliverables and Submitals/Building/4th Submission/3rd Comments/Planning Review 4.9.2026.pdf` — 38 pages; Gemini returned HTTP 503 after a targeted retry.
2. **Record-level review at ingestion:** 150 comments and 14 responses initially required review. The app user subsequently confirmed the remaining batch records on 2026-08-14; see the persisted update below. Document-coverage/completeness QA and the failed source remain separate from record confirmation.
3. **Date completeness:** 53 canonical events have no reliable date. The app should display the date as unavailable rather than inferring one.
4. **Round completeness:** 19 comments lack a reliable effective round. Response round fields are also sparse when the response source itself does not state a round.
5. **PDF highlighting:** page and exact-quote navigation is available, but this batch has no stored coordinate boxes in the web registry. A later locator-only repair can improve highlighting without re-running extraction.

## Post-ingestion manual review update — 2026-08-14

The app-side workbook review was completed and persisted; it was not only browser state. Across the full application, 19 structured workbooks now have a stored `confirmed` decision. Ten of those workbooks belong to this nine-project batch and cover 214 comment rows and 56 response rows. Their records contain a `human_workbook_verification` audit entry.

Workbook confirmation and record/link verification are separate gates. After the workbook reviews, the user explicitly confirmed all remaining record/link-level `needs_review` items in this batch. That decision was persisted as `HR-20260814-new-25-023-031` with per-record `human_record_verification` audit entries.

The current persisted batch now contains **586 confirmed comments, 128 confirmed responses, and 586 verified link rows**. Of the link rows, **128 are confirmed comment-response matches** and **458 are confirmed unmatched comments for which response review is not required**. There are **0 remaining record/link verification rows in `needs_review`** for this batch.

Confirmation does not reactivate duplicate source copies. The scoped batch contains **81 rows with `duplicate_of` provenance**, all of which remain excluded from search. In total, **492 comment rows are search-eligible** and **94 are retained only for audit/source provenance or other production-view exclusions**. The search index was refreshed locally with **0 embedding API calls**. Canonical IDs, issue IDs, relationships, dates, rounds, source locators, and timeline ordering were unchanged by this trust-state update.

## Time and API telemetry

| Measurement | Result |
|---|---:|
| Primary ingestion | 54m 23s |
| Checkpoint-aware recovery | 8m 32s |
| Final single-file retry | 1m 57s |
| Active pipeline time | **1h 04m 51s** |
| First folder touch to dataset-ready checkpoint | **2h 49m 11s** |
| Gemini requests | 138 |
| Completed / definitive failed | 137 / 1 |
| Input tokens | 1,373,779 |
| Cached input tokens | 6,787 |
| Output tokens | 175,390 |
| Thought tokens | 337,384 |
| Images sent | 276 |
| Pages screened / fully analyzed | 256 / 137 |
| Retries | 21 |
| Median / maximum time to first token | 12.54s / 55.22s |
| Median / maximum generation duration | 2.42s / 35.40s |

The largest single straggler was a 4155 Mitzi Drive review PDF at approximately 204 seconds and 290,263 input tokens. The main latency was model wait/time-to-first-token and the size of complex page batches, not local hashing or report generation.

## QA performed

- **67 targeted local tests passed:** evidence-model construction, canonical event/source-occurrence behavior, event deduplication, spreadsheet ingestion, progressive retrieval, and topic taxonomy.
- A broader 153-test run produced 152 passes and one stale frontend assertion. The failing assertion still expects the old `SourcesTrigger` component even though the current application intentionally uses the newer Library-style evidence panel. It is unrelated to this ingestion batch and was not “fixed” by changing unrelated UI code.
- Representative local checks covered a complex PDF extraction, spreadsheet comment-response pairs, multi-event timelines, repeated-history consolidation, date/round provenance, and opaque source registration.

## Final status

The corrected nine-project batch is ingested and available to the application, with verified records separated from uncertain ones. It is **usable but not fully complete** because one 38-page Scharff Avenue source failed and 164 extracted records require review. No already-ingested `new` projects were intentionally reprocessed as part of the corrected scope.
