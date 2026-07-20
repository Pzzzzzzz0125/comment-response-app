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
- Keyword and AI-assisted semantic search.
- Retrieval of historical comments and available responses.
- Classification of results as direct, related, or unverified candidates.
- In-app viewing of PDF, Word, spreadsheet, and CSV source material.
- Navigation to cited pages or spreadsheet locations, with highlighting where source metadata allows it.
- Automated tests covering search, data structure, document previews, permissions, and source behavior.
- Data-quality flags for records that may be incomplete or require review.

The current evaluation results are provisional. A larger domain-reviewed gold dataset is still required before search quality can be treated as production-validated.

## Next Priorities

1. Replace the top-level analysis summary with a conversational, history-grounded AI interface.
2. Document and validate the production data location, configuration, backup, and access model.
3. Define a stable data contract shared by development and production.
4. Complete human or domain review of suggested comment-response matches.
5. Define and validate the canonical issue taxonomy and grouping workflow.
6. Build a larger, domain-approved search evaluation set.
7. Add clear user-visible handling for missing evidence, unverified matches, and incomplete source records.
8. Confirm production security, permissions, monitoring, and operational ownership.

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
