# Comment Response App

An evidence-backed knowledge system for finding, understanding, and reusing historical city permit-review comments and company responses.

## Project Goal

Housing and design projects often go through several review rounds. A city reviews the submitted plans, returns comments, and the project team responds by revising the plans or providing clarification. This process can repeat across properties, cities, and review rounds.

The goal of the Comment Response App is to turn that history into a searchable knowledge base. It should help users understand how similar issues were handled previously while keeping every answer connected to the original records.

The system is intended to let users:

- Ask how similar city comments were handled in past projects.
- Find relevant historical comments and company responses.
- Request summaries, comparisons, and counts from the historical record.
- Review differently worded comments that refer to the same underlying issue.
- Open the supporting source document at the cited location.
- Recognize when no sufficiently relevant historical precedent exists.

The system supports professional review; it does not replace professional judgment or treat an AI-generated answer as an approved permit response.

## Product Principles

### Evidence first

Answers should be grounded in stored historical comments, responses, and source documents. The system should cite the records supporting an answer and distinguish direct precedents from merely related examples.

### Preserve the original record

Similar comments may be grouped under a common issue, but the original wording, property, city, review round, response, and source location must remain available.

### Do not manufacture certainty

The application should be able to say that no sufficiently relevant precedent was found. Missing, suggested, or unverified comment-response links should not be presented as confirmed facts.

### Human review remains necessary

Historical responses may provide useful precedent, but requirements can vary by project, city, code cycle, and reviewer. Users remain responsible for validating any proposed action.

## Intended User Journey

1. The user selects a city or chooses to search across all available history.
2. The user asks a question in the conversational interface.
3. The system identifies whether the user is requesting a precedent search, summary, comparison, or count.
4. The system searches the historical records or calculates the requested result from structured data.
5. It returns a grounded answer with links to the supporting comments and responses.
6. The user opens the matching result set in the comment list.
7. Selecting a comment shows the corresponding historical response, when one is available.
8. Selecting a citation opens the original source document at the relevant page, paragraph, sheet, cell, or range.
9. The user can ask follow-up questions, change the scope, filter the results, or compare projects.

Example questions include:

- How have we handled tree-related comments in previous projects?
- Find comments requiring setback dimensions to be added to the plans. How did we respond?
- How many historical comments concern door width or door size?
- Summarize the most common drainage-related requirements for this city.
- Compare the responses used for similar fire-rated exterior-wall comments.

## Similar-Issue Grouping

Different comments can express the same underlying issue. For example, two reviewers may identify different noncompliant door dimensions while both comments concern the same door-clearance requirement.

The system should support a hierarchy such as:

- Broad topic, such as doors, trees, setbacks, or drainage
- Canonical issue, representing the shared underlying requirement
- Individual historical variants
- Original comments, responses, project details, and source evidence

Grouping should improve discovery and counting without erasing meaningful differences between projects.

## Real Data and Demo Data

The public repository contains only materials that are safe to review publicly:

- Application code
- Automated tests
- Documentation
- Extraction and audit tools
- Synthetic demo data

The public repository does **not** contain:

- Real permit documents
- The real production dataset
- Production embeddings
- The production source registry
- API keys or credentials
- Local filesystem paths

At present, the real permit documents and production data are stored in the local development environment and are excluded from Git through `.gitignore`.

The exact production data root and deployment configuration still need to be documented and confirmed. They should be provided through environment-specific configuration rather than hardcoded into the application or committed to the repository.

## Development and Production Alignment

The proposed environment boundary is:

| Area | Development | Production |
| --- | --- | --- |
| Data | Synthetic fixtures or an approved sanitized subset | Real permit documents and indexed production records |
| Schema | Shared production-compatible schema | Same schema |
| Ingestion | Same validation and import workflow | Same validation and import workflow |
| Source viewer | Demo documents | Authorized real source documents |
| Credentials | Development-only configuration | Production secrets and access controls |

Code behavior, data schemas, identifiers, and validation rules should remain consistent across environments. Only the configured data sources, credentials, and access permissions should differ.

## Current Status

The project is currently a reviewable MVP rather than only a UI prototype.

Completed core capabilities include:

- City-based browsing of historical comments.
- An “Ask Permit History” conversational layer for precedent search, backend-calculated counts, summaries, comparisons, and grounded follow-ups.
- Short-lived result sets that load the exact supporting records into the existing comment list without putting IDs in URLs.
- Keyword and AI-assisted semantic search.
- Retrieval of historical comments and available responses.
- Classification of results as direct, related, or unverified candidates.
- In-app viewing of PDF, Word, spreadsheet, and CSV source material.
- Navigation to cited pages or spreadsheet locations, with highlighting where source metadata allows it.
- Automated tests covering search, data structure, document previews, permissions, and source behavior.
- Data-quality flags for records that may be incomplete or require review.

The current evaluation results are provisional. A larger domain-reviewed gold dataset is still required before search quality can be treated as production-validated.

## Next Priorities

1. Add the proposed canonical-issue clustering and human approval workflow without rewriting original comments.
2. Document and validate the production data location, configuration, backup, and access model.
3. Define a stable data contract shared by development and production.
4. Complete human or domain review of suggested comment-response matches.
5. Build a larger, domain-approved conversational evaluation set.
6. Confirm production security, permissions, monitoring, and operational ownership.

## Conversational API

- `POST /api/knowledge-chat` classifies a question into an allowlisted plan and executes only predefined operations.
- `GET /api/conversations/{conversation_id}` returns the server-held conversation state.
- `GET /api/result-sets/{result_set_id}/comments` hydrates an unexpired result set through the existing comment view model.

Knowledge Chat responses may also include capability-checked `actions`. These
are not free-form questions invented by the model: each action contains an
allowlisted `type`, a short label, the current `result_set_id`, and optional
parameters. The React client renders them as **Explore next** buttons and
posts the selected action back as `guided_action`, preserving the prior result
set while the backend performs the narrower lookup, project comparison,
timeline analysis, response analysis, or unresolved-record filter. Actions are
generated only when the verified result set contains the data needed to carry
them out.

Counts are calculated from unique parent comment IDs in backend code. Gemini cannot provide executable SQL, counts, record IDs, source IDs, or source locations. Semantic answers use only independently verified Direct and Related records; unverified fallback candidates are excluded. Confirmed-response summaries exclude suggested and otherwise unconfirmed response links.

The application enforces a data-trust boundary before building the search index.
Gemini visual-ingestion rows and document-structure rematches are searchable only
when they have `text_trust_status=verified`, a separate `verified_text`, and
`search_eligible=true`. Quarantined text remains in the dataset for audit but is
excluded from city summaries, keyword search, Smart Search, Knowledge Chat, and
normal comment display.

Import the manually verified all-project paired and unpaired workbook with a
conflict-reporting dry run followed by the atomic import:

```sh
python3 web_app/import_all_projects_rematch.py /path/to/all_projects_comment_response_rematch.xlsx
python3 web_app/import_all_projects_rematch.py /path/to/all_projects_comment_response_rematch.xlsx --apply
```

The importer requires exact Comment IDs and source text, never uses semantic
matching, preserves raw comment text, supports verified grouped responses, and
stores both XML and visible DOCX paragraph indices. Repeating the import creates
no additional responses or links.

Knowledge Chat routing and grounded answer summaries use `gemini-3.6-flash` by default through `KNOWLEDGE_GEMINI_MODEL` or `--knowledge-gemini-model`. It shares the server-side API key but uses a separate client from Smart Search, whose model remains controlled by `GEMINI_MODEL` or `--gemini-model`. Semantic retrieval inside a conversation continues to use the existing Smart Search client.

Source files remain available only through authorized in-app preview and spreadsheet endpoints. The public source model exposes no original-download action, and `/api/documents/{document_id}/original` is disabled.

## Common Topic Document Identity

Common Topics are counted only across independent logical documents. The
canonicalization pass separates each physical `source_file` from its
`canonical_document`: identical SHA-256 files, normalized-content re-exports,
and renamed/archive copies are grouped as aliases. Repeated extraction rows
inside one canonical document contribute one comment occurrence at most.

The persisted dataset contains `source_files`, `canonical_documents`,
`source_file_aliases`, and `near_duplicate_review`. Run the explicit repair
command after an older dataset is imported:

```bash
python3 web_app/canonicalize_documents.py --apply
```

Near-duplicate documents with 90–98% substantive overlap are retained as
separate documents and listed for review; only confirmed new or reissued
comments contribute to topic frequency. The city summary reports independent
source documents and the number of physical duplicate files excluded.

### Global same-event deduplication

Run the explicit repair after a batch import or after adding a new source
folder:

```bash
python3 web_app/deduplicate_comments.py --apply
python3 web_app/migrate_sources.py
python3 web_app/build_search_index.py --metadata-only
```

The repair keeps one production record for a same-site, same-review-round,
same-date, same-normalized-text event. Immutable duplicate rows remain in
`dataset.json` with `duplicate_of` and `search_eligible=false`. Their page,
sheet, cell, paragraph, and file locators are copied to the canonical record's
`source_occurrences`, so the source viewer exposes every supporting file. A
different date, review round, site, or parameterized requirement remains a
separate event.

## Cross-round issue timelines

The app treats a continuing design issue as one issue thread across all sites,
disciplines, and submission rounds. A thread is keyed from the normalized site,
discipline, and issue identity (`issue_thread_id`); it is not limited to one
document or to the Nature project.

Rows repeated by extraction within the same source submission are suppressed.
Rows from a later numbered submission are retained as additional members of the
same thread, so the history shows what was requested, what was answered, and
what remained open at each point in time. Distinct sites, distinct issues, and
meaningfully different parameterized requirements remain separate threads.

The detail view displays one representative issue card in the results list and
combines its immutable source records into a chronological timeline. Dates are
shown from the most reliable available evidence: reviewer/discussion date,
document filename date, submission label, or workbook export date. Original
comment and response text is not overwritten when a later round is added.

Within a thread, events with the same event type, calendar date, and normalized
body are displayed once even when they were extracted from multiple files. The
canonical event keeps every source occurrence, so the UI can show one event with
multiple source buttons. Events without a reliable date are kept separate to
avoid accidental merging. The repair command stores this audit index in
`issue_event_index` while leaving each raw extraction row unchanged.

## Frontend Architecture

The browser interface is a React 19 + TypeScript application in `frontend/`.
Flask-compatible Python HTTP handling remains responsible for data, Gemini,
retrieval, ingestion, authorization, and file delivery. Vite builds the React
application into `web_app/static/`, which the existing Python server serves as
its single-page frontend.

The component layers are:

- `frontend/src/components/ui/`: shadcn/ui source components and theme primitives.
- `frontend/src/components/ai-elements/`: AI Elements conversation, message,
  prompt, suggestion, and source components.
- `frontend/src/components/knowledge-chat.tsx`: the existing knowledge-chat API
  rendered as a source-grounded conversational experience.
- `frontend/src/components/history-results.tsx`: keyword/Smart Search results,
  timeline-aware filters, selection, and collapsible retrieval explanations.
  The former manual categorization controls are intentionally no longer exposed
  in the history library.
- `frontend/src/components/comment-detail.tsx`: responsive, resizable comparison
  of government comments and historical company responses.
- `frontend/src/components/source-viewer.tsx`: authorized PDF and spreadsheet
  viewing with cited-page navigation, coordinate/text highlighting, and cited
  range selection.
- `frontend/src/components/review-links-dialog.tsx`: the existing response-link
  review workflow.

No Gemini key or document filesystem path is included in the frontend bundle.

### Build and test the frontend

Node.js 18 or newer is required. From `frontend/`:

```bash
npm install
npm test
npm run build
```

Commit `package-lock.json` so dependency resolution is reproducible. The built
files in `web_app/static/` let reviewers run the app without starting a separate
Vite process. During frontend development, `npm run dev` may be used separately;
API proxying is not currently configured, so the production build remains the
default integrated workflow.

## Scope Boundaries

The application is designed to retrieve and summarize historical evidence. It should not:

- Invent citations, page numbers, spreadsheet locations, or file paths.
- Present a related example as a direct precedent.
- Calculate counts from an AI estimate when the value can be computed from the database.
- Hide uncertainty about incomplete or unverified records.
- Automatically treat a historical response as correct for a new project without review.

## Repository

Public review repository: [Pzzzzzzz0125/comment-response-app](https://github.com/Pzzzzzzz0125/comment-response-app)

The repository is intentionally separated from real production documents and private production data.

## Open Documentation Items

The following information should be added after it is confirmed:

- Exact production data root and configuration owner
- Production deployment environment and release process
- Backup and recovery procedure
- Authentication roles and document-access rules
- Data-retention and deletion requirements
- Approved taxonomy owner and review process
- Production acceptance criteria and domain evaluation thresholds
