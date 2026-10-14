# Architecture

## Product shape

Marked Register is one local web application with a background processing path:

```text
Browser
  -> FastAPI + server-rendered HTML
  -> application workflow
  -> neutral register pipeline
  -> reviewable entries and exports
```

The web interface and CSV contract are product interfaces. Internal OCR,
storage, and execution choices must not leak into them.

## Local deployment

- FastAPI serves HTML with lightweight HTMX or JavaScript enhancements.
- SQLAlchemy 2 and Alembic manage structured state in SQLite.
- A user-selected job directory holds source PDFs, rendered pages, crops,
  diagnostics, and exports as ordinary files.
- One background job runs at a time because local inference is resource-heavy.
- Queued, running, page-progress, complete, and failed states persist in SQLite.
  Interrupted work becomes an explicit retryable failure after restart; retry
  replaces deterministic artifacts and entry metadata instead of appending rows.
- GLM-OCR is the text recogniser. The application runs it in a private
  llama.cpp server on the loopback interface, so recognition uses whichever
  accelerator the installed llama.cpp build supports and needs no
  platform-specific Python dependencies.

Large binary artifacts are not stored in SQLite. Database records reference
relative artifact paths and content hashes so a job directory remains movable
and auditable.

Unknown page structures never enter the extraction pipeline silently. Layout
analysis persists an annotated diagnostic, and only an explicit reviewer
exclusion lets the remaining confirmed pages of that PDF continue.

## Deep pipeline seam

The neutral pipeline accepts confirmed source PDFs and a layout/interpretation
profile. It returns observable entries, evidence, diagnostics, and review
requirements. It never returns campaign actions.

The pipeline keeps these concepts separate:

1. visible evidence, such as a dash or strikethrough;
2. neutral register status derived from an explicit interpretation profile;
3. the reviewer's final decision.

No missing elector number may be silently inferred as accepted source truth.

## Deployment-ready seams

The product is local-only today, but eventual audited hosting is a real goal.
Four narrow internal seams keep that path open:

| Concern | Local adapter | Possible hosted adapter |
| --- | --- | --- |
| Metadata | SQLite | PostgreSQL |
| Artifacts | Local filesystem | Private S3-compatible storage |
| Recognition | GLM-OCR on local llama.cpp | Audited inference provider |
| Jobs | Local process | Queue-backed worker |

Only local adapters are implemented for the MVP. The application does not add a
cloud control plane, provider-specific code, multi-tenancy, or premature hosted
security machinery.

An initial hosted deployment should be single-tenant and container-based. A
conventional application platform is a better fit for the Python pipeline than
Cloudflare Workers. Cloudflare R2 or other S3-compatible storage may still be a
useful artifact adapter later.

## Test seams

Tests observe behaviour through three agreed interfaces:

1. the local web workflow;
2. neutral processing from a PDF job to reviewable elector records;
3. reviewed CSV exports.

Committed acceptance fixtures are synthetic. Real Hackney documents remain
outside the repository and are exercised only by an ignored local validation
suite.
