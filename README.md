# Marked Register

Marked Register is a local-first web application for digitising UK polling
station marked-register PDFs. It helps a human reviewer replace a manual paper
workflow with auditable extraction and rapid keyboard validation.

The project is deliberately neutral. It records register observations and
in-person voting status; it does not make campaign targeting decisions.

The application is at an early alpha stage and has only been validated against
Hackney register layouts. Do not treat unreviewed output as authoritative.

## Product contract

The MVP workflow, output contract, privacy rules, and exclusions are defined in
[`docs/product-contract.md`](docs/product-contract.md). Architecture decisions
are recorded in [`docs/architecture.md`](docs/architecture.md).

## Development

Requires Python 3.12 and `uv`.

The local processing adapter also requires the Tesseract executable. On macOS:

```bash
brew install tesseract
```

```bash
uv sync --extra local-ai
uv run pytest
uv run ruff check .
```

Start the local application with:

```bash
uv run marked-register
```

It listens on `127.0.0.1:8000` and stores the application database under
`.local/` unless `--database` selects another path. Each review job stores its
sensitive PDFs and artifacts in the directory chosen in the browser.

The reviewer works entirely in the browser: add PDFs, confirm the polling
district and annotated layouts, run the serialized local processor, validate
every row with the documented shortcuts, then create the all-elector and
voted-only CSVs. Processing failures and interrupted runs remain visible and
retryable after restart.

The operational CSV columns are, in order:

```text
electoral_reference,polling_district,elector_number,register_status,printed_marker,review_outcome,review_note,source_file,page_number,source_entry_id
```

An accompanying JSON manifest records provenance and file hashes. Any later
source, layout, or review change removes the derived exports so stale files
cannot be downloaded from the application.

No real register PDFs, page images, crops, OCR text, names, or addresses may be
committed to this repository.
