import csv
import json
import re
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from marked_register.processing import Anchor, EntryRecognition, LocalRegisterEngine
from marked_register.web import create_app


class SyntheticAnchorDetector:
    def detect(
        self,
        image,  # noqa: ANN001
        *,
        column: str,
        bbox: tuple[int, int, int, int],
    ) -> list[Anchor]:
        del bbox
        label = "100" if column == "L" else "101"
        return [Anchor(label=label, y_center=round(image.shape[0] * 140 / 842), confidence=99)]


class SyntheticEntryRecognizer:
    model_name = "synthetic-recognizer"

    def recognize(self, image, *, detector_label: str) -> EntryRecognition:  # noqa: ANN001
        del image
        return EntryRecognition(elector_number=detector_label, printed_marker=None)


def synthetic_engine() -> LocalRegisterEngine:
    return LocalRegisterEngine(
        anchor_detector=SyntheticAnchorDetector(),
        entry_recognizer=SyntheticEntryRecognizer(),
        dpi=300,
    )


def create_job(client: TestClient, workspace_path: Path) -> str:
    response = client.post(
        "/jobs",
        data={"name": "Hackney review", "workspace_path": str(workspace_path)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"]


def synthetic_register_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((48, 48), "SYNTHETIC MARKED REGISTER — DISTRICT HSLA")
    page.draw_line((30, 100), (565, 100), width=1)
    page.insert_text((48, 140), "100")
    page.insert_text((120, 140), "SYNTHETIC ENTRY")
    page.draw_line((72, 136), (98, 136), width=2)
    page.insert_text((345, 140), "101")
    page.insert_text((417, 140), "SYNTHETIC ENTRY")
    page.draw_line((30, 760), (565, 760), width=1)
    document.save(path)
    document.close()


def test_reviewer_can_open_the_job_start_page(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Create a review job" in response.text
    assert 'name="name"' in response.text
    assert 'name="workspace_path"' in response.text


def test_reviewer_can_create_and_reopen_a_job(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "hackney-review"

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        job_url = create_job(client, workspace_path)
        created_page = client.get(job_url)
        assert created_page.status_code == 200
        assert "Hackney review" in created_page.text
        assert "Add marked-register PDFs" in created_page.text

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        reopened_page = client.get(job_url)

    assert reopened_page.status_code == 200
    assert "Hackney review" in reopened_page.text
    assert workspace_path.is_dir()


def test_reviewer_can_add_a_synthetic_register_pdf(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "hackney-review"
    pdf_path = tmp_path / "HSLA.pdf"
    synthetic_register_pdf(pdf_path)

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        job_url = create_job(client, workspace_path)
        with pdf_path.open("rb") as pdf:
            response = client.post(
                f"{job_url}/sources",
                files={"pdf": (pdf_path.name, pdf, "application/pdf")},
                follow_redirects=False,
            )

        assert response.status_code == 303

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        reopened_page = client.get(job_url)

    assert reopened_page.status_code == 200
    assert "HSLA.pdf" in reopened_page.text
    assert "Polling district needs confirmation" in reopened_page.text


def test_reviewer_can_confirm_a_source_polling_district(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "hackney-review"
    pdf_path = tmp_path / "HSLA.pdf"
    synthetic_register_pdf(pdf_path)

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        job_url = create_job(client, workspace_path)
        with pdf_path.open("rb") as pdf:
            client.post(
                f"{job_url}/sources",
                files={"pdf": (pdf_path.name, pdf, "application/pdf")},
            )

        job_page = client.get(job_url)
        match = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/polling-district)"',
            job_page.text,
        )
        assert match is not None
        response = client.post(
            match.group("url"),
            data={"polling_district": "hsla"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        reopened_page = client.get(job_url)

    assert "Polling district HSLA" in reopened_page.text
    assert "Polling district needs confirmation" not in reopened_page.text


def test_reviewer_can_generate_persistent_layout_previews(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "hackney-review"
    pdf_path = tmp_path / "HSLA.pdf"
    synthetic_register_pdf(pdf_path)

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        job_url = create_job(client, workspace_path)
        with pdf_path.open("rb") as pdf:
            client.post(
                f"{job_url}/sources",
                files={"pdf": (pdf_path.name, pdf, "application/pdf")},
            )
        job_page = client.get(job_url)
        district_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/polling-district)"',
            job_page.text,
        )
        assert district_form is not None
        client.post(district_form.group("url"), data={"polling_district": "HSLA"})

        job_page = client.get(job_url)
        analysis_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-analysis)"',
            job_page.text,
        )
        assert analysis_form is not None
        response = client.post(analysis_form.group("url"), follow_redirects=False)
        assert response.status_code == 303

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        preview_page = client.get(job_url)
        assert preview_page.status_code == 200
        assert "Review page layouts" in preview_page.text
        assert "Page 1" in preview_page.text
        assert "Awaiting layout confirmation" in preview_page.text
        preview_url = re.search(r'src="(?P<url>[^\"]+/layout-preview)"', preview_page.text)
        assert preview_url is not None
        preview_response = client.get(preview_url.group("url"))
        assert preview_response.status_code == 200
        assert preview_response.headers["content-type"] == "image/png"


def test_reviewer_can_confirm_detected_page_layouts(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "hackney-review"
    pdf_path = tmp_path / "HSLA.pdf"
    synthetic_register_pdf(pdf_path)

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        job_url = create_job(client, workspace_path)
        with pdf_path.open("rb") as pdf:
            client.post(
                f"{job_url}/sources",
                files={"pdf": (pdf_path.name, pdf, "application/pdf")},
            )
        job_page = client.get(job_url)
        district_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/polling-district)"',
            job_page.text,
        )
        assert district_form is not None
        client.post(district_form.group("url"), data={"polling_district": "HSLA"})
        job_page = client.get(job_url)
        analysis_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-analysis)"',
            job_page.text,
        )
        assert analysis_form is not None
        client.post(analysis_form.group("url"))

        job_page = client.get(job_url)
        confirmation_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-confirmation)"',
            job_page.text,
        )
        assert confirmation_form is not None
        response = client.post(
            confirmation_form.group("url"),
            data={"decision": "confirmed"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with TestClient(create_app(database_url=f"sqlite+pysqlite:///{database_path}")) as client:
        confirmed_page = client.get(job_url)

    assert "Layout confirmed" in confirmed_page.text
    assert "Process register" in confirmed_page.text
    assert "Awaiting layout confirmation" not in confirmed_page.text


def test_confirmed_source_can_be_processed_into_persistent_entries(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "hackney-review"
    pdf_path = tmp_path / "HSLA.pdf"
    synthetic_register_pdf(pdf_path)
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = create_job(client, workspace_path)
        with pdf_path.open("rb") as pdf:
            client.post(
                f"{job_url}/sources",
                files={"pdf": (pdf_path.name, pdf, "application/pdf")},
            )
        job_page = client.get(job_url)
        district_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/polling-district)"',
            job_page.text,
        )
        assert district_form is not None
        client.post(district_form.group("url"), data={"polling_district": "HSLA"})
        job_page = client.get(job_url)
        analysis_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-analysis)"',
            job_page.text,
        )
        assert analysis_form is not None
        client.post(analysis_form.group("url"))
        job_page = client.get(job_url)
        confirmation_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-confirmation)"',
            job_page.text,
        )
        assert confirmation_form is not None
        client.post(confirmation_form.group("url"), data={"decision": "confirmed"})

        job_page = client.get(job_url)
        processing_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/processing)"',
            job_page.text,
        )
        assert processing_form is not None
        response = client.post(processing_form.group("url"), follow_redirects=False)
        assert response.status_code == 303

    with TestClient(create_app(**app_args)) as client:
        processed_page = client.get(job_url)

    assert "2 entries ready for review" in processed_page.text
    assert "Review entries" in processed_page.text


def prepare_processed_job(client: TestClient, tmp_path: Path) -> str:
    workspace_path = tmp_path / "review-workspace"
    pdf_path = tmp_path / "review-source.pdf"
    synthetic_register_pdf(pdf_path)
    job_url = create_job(client, workspace_path)
    with pdf_path.open("rb") as pdf:
        client.post(
            f"{job_url}/sources",
            files={"pdf": (pdf_path.name, pdf, "application/pdf")},
        )
    job_page = client.get(job_url)
    district_form = re.search(
        r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/polling-district)"',
        job_page.text,
    )
    assert district_form is not None
    client.post(district_form.group("url"), data={"polling_district": "HSLA"})
    job_page = client.get(job_url)
    analysis_form = re.search(
        r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-analysis)"',
        job_page.text,
    )
    assert analysis_form is not None
    client.post(analysis_form.group("url"))
    job_page = client.get(job_url)
    confirmation_form = re.search(
        r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/layout-confirmation)"',
        job_page.text,
    )
    assert confirmation_form is not None
    client.post(confirmation_form.group("url"), data={"decision": "confirmed"})
    job_page = client.get(job_url)
    processing_form = re.search(
        r'action="(?P<url>/jobs/[^\"]+/sources/[^\"]+/processing)"',
        job_page.text,
    )
    assert processing_form is not None
    client.post(processing_form.group("url"))
    return job_url


def test_reviewer_can_confirm_every_entry_with_one_action(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        assert first_page.status_code == 200
        assert "0 of 2 reviewed" in first_page.text
        assert "Elector 100" in first_page.text
        assert "Space" in first_page.text
        crop_url = re.search(r'src="(?P<url>[^\"]+/entry-crop)"', first_page.text)
        assert crop_url is not None
        assert client.get(crop_url.group("url")).headers["content-type"] == "image/png"
        review_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            first_page.text,
        )
        assert review_form is not None
        response = client.post(
            review_form.group("url"),
            data={"action": "accept", "elector_number": "100", "note": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303

        second_page = client.get(response.headers["location"])
        assert "1 of 2 reviewed" in second_page.text
        assert "Elector 101" in second_page.text
        review_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            second_page.text,
        )
        assert review_form is not None
        response = client.post(
            review_form.group("url"),
            data={"action": "accept", "elector_number": "101", "note": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with TestClient(create_app(**app_args)) as client:
        completed_page = client.get(f"{job_url}/review")

    assert completed_page.status_code == 200
    assert "Review complete" in completed_page.text
    assert "2 of 2 reviewed" in completed_page.text


def test_reviewer_can_correct_and_revisit_an_entry(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        review_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            first_page.text,
        )
        assert review_form is not None
        response = client.post(
            review_form.group("url"),
            data={
                "action": "struck_through",
                "elector_number": "100/1",
                "note": "Checked against the page",
            },
            follow_redirects=False,
        )
        second_page = client.get(response.headers["location"])
        previous_url = re.search(
            r'id="previous-entry" href="(?P<url>[^\"]+)"',
            second_page.text,
        )
        assert previous_url is not None
        revisited_page = client.get(previous_url.group("url"))

    assert "Elector 100/1" in revisited_page.text
    assert "Decision: Corrected" in revisited_page.text
    assert "Status: Struck through" in revisited_page.text
    assert 'value="Checked against the page"' in revisited_page.text


def test_unresolved_and_rejected_rows_are_explicitly_accounted_for(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        first_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            first_page.text,
        )
        assert first_form is not None
        response = client.post(
            first_form.group("url"),
            data={"action": "unresolved", "elector_number": "100", "note": "Ambiguous"},
            follow_redirects=False,
        )
        second_page = client.get(response.headers["location"])
        second_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            second_page.text,
        )
        assert second_form is not None
        response = client.post(
            second_form.group("url"),
            data={"action": "reject", "elector_number": "101", "note": "False row"},
            follow_redirects=False,
        )
        completed_page = client.get(response.headers["location"])

    assert "Review complete" in completed_page.text
    assert "1 unresolved" in completed_page.text
    assert "1 rejected" in completed_page.text


def test_reviewer_can_add_a_missed_elector_from_page_context(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        manual_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/manual-entry)"',
            first_page.text,
        )
        assert manual_form is not None
        response = client.post(
            manual_form.group("url"),
            data={
                "elector_number": "100/1",
                "register_status": "voted_in_person",
                "note": "Missed between printed rows",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        added_page = client.get(response.headers["location"])

    assert "1 of 3 reviewed" in added_page.text
    assert "Elector 100/1" in added_page.text
    assert "Manually added from page context" in added_page.text
    assert "Decision: Corrected" in added_page.text
    assert "Status: Voted in person" in added_page.text


def test_reviewer_can_undo_a_previous_decision(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        first_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            first_page.text,
        )
        assert first_form is not None
        response = client.post(
            first_form.group("url"),
            data={"action": "accept", "elector_number": "100", "note": ""},
            follow_redirects=False,
        )
        second_page = client.get(response.headers["location"])
        previous_url = re.search(
            r'id="previous-entry" href="(?P<url>[^\"]+)"',
            second_page.text,
        )
        assert previous_url is not None
        previous_page = client.get(previous_url.group("url"))
        assert "Backspace</kbd> Undo decision" in previous_page.text
        response = client.post(
            first_form.group("url"),
            data={"action": "undo", "elector_number": "100", "note": ""},
            follow_redirects=False,
        )
        reopened_page = client.get(response.headers["location"])

    assert "0 of 2 reviewed" in reopened_page.text
    assert "Elector 100" in reopened_page.text
    assert "Decision:" not in reopened_page.text


def test_exports_are_blocked_until_every_entry_is_reviewed(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        response = client.post(f"{job_url}/exports")

    assert response.status_code == 409
    assert response.json()["detail"] == "2 entries still need review"


def test_reviewed_job_produces_canonical_and_voted_only_exports(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "review-workspace"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        first_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            first_page.text,
        )
        assert first_form is not None
        response = client.post(
            first_form.group("url"),
            data={
                "action": "voted_in_person",
                "elector_number": "100/1",
                "note": "Corrected from page",
            },
            follow_redirects=False,
        )
        second_page = client.get(response.headers["location"])
        second_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            second_page.text,
        )
        assert second_form is not None
        client.post(
            second_form.group("url"),
            data={"action": "accept", "elector_number": "101", "note": ""},
        )

        response = client.post(f"{job_url}/exports", follow_redirects=False)
        assert response.status_code == 303
        job_page = client.get(response.headers["location"])

    columns = [
        "electoral_reference",
        "polling_district",
        "elector_number",
        "register_status",
        "printed_marker",
        "review_outcome",
        "review_note",
        "source_file",
        "page_number",
        "source_entry_id",
    ]
    with (workspace_path / "exports" / "all-electors.csv").open(newline="") as file:
        all_reader = csv.DictReader(file)
        all_rows = list(all_reader)
    with (workspace_path / "exports" / "voted-only.csv").open(newline="") as file:
        voted_reader = csv.DictReader(file)
        voted_rows = list(voted_reader)
    manifest = json.loads((workspace_path / "exports" / "manifest.json").read_text())

    assert all_reader.fieldnames == columns
    assert voted_reader.fieldnames == columns
    assert [row["electoral_reference"] for row in all_rows] == ["HSLA-100/1", "HSLA-101"]
    assert [row["register_status"] for row in all_rows] == [
        "voted_in_person",
        "no_in_person_mark",
    ]
    assert [row["review_outcome"] for row in all_rows] == ["corrected", "confirmed"]
    assert all_rows[0]["review_note"] == "Corrected from page"
    assert all(row["source_file"] == "review-source.pdf" for row in all_rows)
    assert len(voted_rows) == 1
    assert voted_rows[0]["electoral_reference"] == "HSLA-100/1"
    assert manifest["schema_version"] == 1
    assert manifest["review_summary"] == {
        "extracted_rows": 2,
        "exported_rows": 2,
        "rejected_rows": 0,
        "unresolved_rows": 0,
    }
    assert manifest["sources"][0]["sha256"]
    assert manifest["recognizer_models"] == ["synthetic-recognizer"]
    assert "Download all electors CSV" in job_page.text
    assert "Download voted-only CSV" in job_page.text
    assert "Download export manifest" in job_page.text


def test_export_includes_unresolved_and_excludes_rejected_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite"
    workspace_path = tmp_path / "review-workspace"
    app_args = {
        "database_url": f"sqlite+pysqlite:///{database_path}",
        "register_engine": synthetic_engine(),
    }

    with TestClient(create_app(**app_args)) as client:
        job_url = prepare_processed_job(client, tmp_path)
        first_page = client.get(f"{job_url}/review")
        first_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            first_page.text,
        )
        assert first_form is not None
        response = client.post(
            first_form.group("url"),
            data={"action": "unresolved", "elector_number": "100", "note": "Unreadable"},
            follow_redirects=False,
        )
        second_page = client.get(response.headers["location"])
        second_form = re.search(
            r'action="(?P<url>/jobs/[^\"]+/entries/[^\"]+/review)"',
            second_page.text,
        )
        assert second_form is not None
        client.post(
            second_form.group("url"),
            data={"action": "reject", "elector_number": "101", "note": "False row"},
        )
        client.post(f"{job_url}/exports")

    with (workspace_path / "exports" / "all-electors.csv").open(newline="") as file:
        rows = list(csv.DictReader(file))
    with (workspace_path / "exports" / "voted-only.csv").open(newline="") as file:
        voted_rows = list(csv.DictReader(file))
    manifest = json.loads((workspace_path / "exports" / "manifest.json").read_text())

    assert len(rows) == 1
    assert rows[0]["electoral_reference"] == "HSLA-100"
    assert rows[0]["register_status"] == "unresolved"
    assert rows[0]["review_outcome"] == "unresolved"
    assert voted_rows == []
    assert manifest["review_summary"] == {
        "extracted_rows": 2,
        "exported_rows": 1,
        "rejected_rows": 1,
        "unresolved_rows": 1,
    }
