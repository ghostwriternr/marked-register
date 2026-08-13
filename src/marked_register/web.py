"""Local web application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from marked_register.jobs import Jobs, LocalSourceFileStore
from marked_register.layouts import Layouts, LocalLayoutOverlayStore
from marked_register.sql import SqlAlchemyJobRepository, application_engine, upgrade_database

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


def create_app(*, database_url: str) -> FastAPI:
    """Create the local Marked Register web application."""
    engine = application_engine(database_url)
    jobs = Jobs(SqlAlchemyJobRepository(engine), LocalSourceFileStore())
    layouts = Layouts(SqlAlchemyJobRepository(engine), LocalLayoutOverlayStore())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        upgrade_database(database_url)
        yield
        engine.dispose()

    app = FastAPI(title="Marked Register", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.layouts = layouts

    @app.get("/", response_class=HTMLResponse)
    def start_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="start.html",
            context={},
        )

    @app.post("/jobs")
    def create_job(
        request: Request,
        name: Annotated[str, Form()],
        workspace_path: Annotated[Path, Form()],
    ) -> RedirectResponse:
        job = request.app.state.jobs.create(name=name, workspace_path=workspace_path)
        return RedirectResponse(
            url=f"/jobs/{job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def show_job(request: Request, job_id: str) -> HTMLResponse:
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="job.html",
            context={
                "job": job,
                "sources": [
                    {
                        "document": source,
                        "layouts": request.app.state.layouts.previews_for_source(source.id),
                    }
                    for source in request.app.state.jobs.sources_for_job(job_id)
                ],
            },
        )

    @app.post("/jobs/{job_id}/sources")
    async def add_source(
        request: Request,
        job_id: str,
        pdf: Annotated[UploadFile, File()],
    ) -> RedirectResponse:
        try:
            request.app.state.jobs.add_source(
                job_id=job_id,
                filename=pdf.filename or "",
                content=await pdf.read(),
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/sources/{source_id}/polling-district")
    def confirm_polling_district(
        request: Request,
        job_id: str,
        source_id: str,
        polling_district: Annotated[str, Form()],
    ) -> RedirectResponse:
        try:
            request.app.state.jobs.confirm_polling_district(
                job_id=job_id,
                source_id=source_id,
                polling_district=polling_district,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/sources/{source_id}/layout-analysis")
    def analyze_layout(request: Request, job_id: str, source_id: str) -> RedirectResponse:
        try:
            request.app.state.layouts.analyze_source(job_id=job_id, source_id=source_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}/layout-previews/{preview_id}/layout-preview")
    def layout_preview(request: Request, job_id: str, preview_id: str) -> FileResponse:
        try:
            path = request.app.state.layouts.preview_path(
                job_id=job_id,
                preview_id=preview_id,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return FileResponse(path, media_type="image/png")

    @app.post("/jobs/{job_id}/sources/{source_id}/layout-confirmation")
    def confirm_layout(
        request: Request,
        job_id: str,
        source_id: str,
        decision: Annotated[str, Form()],
    ) -> RedirectResponse:
        try:
            request.app.state.layouts.review_source(
                job_id=job_id,
                source_id=source_id,
                decision=decision,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return app
