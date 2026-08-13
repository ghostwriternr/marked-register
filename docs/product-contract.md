# Product contract

## Purpose

Marked Register turns polling-station marked-register PDFs into a reviewed,
neutral ledger of electors and visible register marks. It is designed for a
non-technical reviewer who previously performed the entire task manually on
paper.

The MVP runs locally on Apple Silicon. Every PDF, rendered page, crop, model
result, and review decision remains on the operator's machine.

## Workflow

1. Create a persistent job in a user-chosen local directory.
2. Add one or more marked-register PDFs.
3. Confirm or correct the detected polling district for every PDF.
4. Validate annotated page layouts before full processing.
5. Process the PDFs into reviewable elector entries.
6. Review every entry in physical document order.
7. Confirm, correct, reject, add, or leave entries explicitly unresolved.
8. Export only after every entry has a human review outcome.

Review follows source file, page, left column, right column, then top to bottom.
The primary action accepts the prediction and advances with one keystroke.

## User-facing statuses

- `Voted in person`
- `No in-person vote mark found`
- `Struck through`
- `Needs review`

`Struck through` is an observed register state. It does not assert that an
elector voted by post or explain why they could not vote in person.

## Review controls

| Key | Action |
| --- | --- |
| `Space` | Accept the prediction and advance |
| `1` | Mark voted in person |
| `2` | Mark no in-person vote mark |
| `3` | Mark struck through |
| `4` | Mark unresolved |
| `E` | Edit the elector number |
| `N` | Add a review note |
| `Backspace` | Undo or return to the previous entry |

The reviewer can also reject a false row and add a missed elector from page
context. Every decision is saved immediately. An unresolved decision counts as
reviewed; an untouched entry does not.

## CSV contract

The canonical export contains every reviewed elector. A convenience export uses
the same columns and includes only `voted_in_person` rows.

| Column | Meaning |
| --- | --- |
| `electoral_reference` | Polling district and elector number, for example `HSLA-1478` |
| `polling_district` | Confirmed polling district |
| `elector_number` | Reviewed elector number |
| `register_status` | `voted_in_person`, `no_in_person_mark`, `struck_through`, or `unresolved` |
| `printed_marker` | Visible printed marker, when present |
| `review_outcome` | `confirmed`, `corrected`, or `unresolved` |
| `review_note` | Optional human note |
| `source_file` | Original PDF filename |
| `page_number` | One-based source page number |
| `source_entry_id` | Stable identifier connecting the row to local evidence |

Technical evidence, geometry, model versions, source hashes, and diagnostics
belong in the machine-readable job record rather than the operational CSV.

## Privacy

Real voter data must never enter the Git repository, fixtures, logs, telemetry,
screenshots, documentation, or hosted services. Committed tests use synthetic,
PII-free documents. Local Hackney validation data is referenced only through
ignored local configuration.

## MVP exclusions

- names and addresses;
- postal-register imports;
- campaign targeting or prioritisation;
- eCanvasser, NationBuilder, or other campaign integrations;
- official-turnout reconciliation;
- interactive layout-profile editing;
- cross-platform support;
- polished installation or application packaging;
- hosted processing.

The alpha supports validated Hackney layouts. Unknown structures must stop with
clear annotated diagnostics rather than produce silent best-effort output.
