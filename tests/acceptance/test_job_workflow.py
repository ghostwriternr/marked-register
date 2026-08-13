import re
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from marked_register.web import create_app


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
    page.draw_line((82, 136), (108, 136), width=2)
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
