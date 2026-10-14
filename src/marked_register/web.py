"""Local web application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from marked_register.exports import ExportNotReadyError, Exports
from marked_register.jobs import Jobs, LocalSourceFileStore
from marked_register.layouts import Layouts, LocalLayoutOverlayStore
from marked_register.processing import (
    GlmEntryRecognizer,
    LocalProcessingQueue,
    LocalRegisterEngine,
    Processing,
    ProcessingQueue,
    RegisterEngine,
    TesseractAnchorDetector,
)
from marked_register.reviews import (
    Reviews,
    effective_elector_number,
    effective_register_status,
    status_label,
    warning_label,
)
from marked_register.sql import SqlAlchemyJobRepository, application_engine, upgrade_database

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


def create_app(
    *,
    database_url: str,
    register_engine: RegisterEngine | None = None,
    processing_queue: ProcessingQueue | None = None,
) -> FastAPI:
    """Create the local Marked Register web application."""
    engine = application_engine(database_url)
    jobs = Jobs(SqlAlchemyJobRepository(engine), LocalSourceFileStore())
    layouts = Layouts(SqlAlchemyJobRepository(engine), LocalLayoutOverlayStore())
    entry_recognizer: GlmEntryRecognizer | None = None
    if register_engine is None:
        entry_recognizer = GlmEntryRecognizer()
        register_engine = LocalRegisterEngine(
            anchor_detector=TesseractAnchorDetector(),
            entry_recognizer=entry_recognizer,
        )
    processing = Processing(SqlAlchemyJobRepository(engine), register_engine)
    resolved_processing_queue = processing_queue or LocalProcessingQueue()
    reviews = Reviews(SqlAlchemyJobRepository(engine))
    exports = Exports(SqlAlchemyJobRepository(engine))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        upgrade_database(database_url)
        SqlAlchemyJobRepository(engine).recover_interrupted_processing()
        yield
        if entry_recognizer is not None:
            entry_recognizer.close()
        engine.dispose()

    app = FastAPI(title="Marked Register", lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.state.jobs = jobs
    app.state.layouts = layouts
    app.state.processing = processing
    app.state.processing_queue = resolved_processing_queue
    app.state.reviews = reviews
    app.state.exports = exports

    @app.exception_handler(HTTPException)
    async def friendly_http_error(request: Request, error: HTTPException) -> Response:
        if "text/html" not in request.headers.get("accept", ""):
            return await http_exception_handler(request, error)
        job_id = request.path_params.get("job_id")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "detail": error.detail,
                "back_url": f"/jobs/{job_id}" if job_id else "/",
            },
            status_code=error.status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def start_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name="start.html",
            context={"recent_jobs": request.app.state.jobs.recent()},
        )

    @app.post("/jobs")
    def create_job(
        request: Request,
        name: Annotated[str, Form()],
        workspace_path: Annotated[Path, Form()],
    ) -> RedirectResponse:
        try:
            job = request.app.state.jobs.create(name=name, workspace_path=workspace_path)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            url=f"/jobs/{job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def show_job(request: Request, job_id: str) -> HTMLResponse:
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        sources = request.app.state.jobs.sources_for_job(job_id)
        source_items = [
            {
                "document": source,
                "layouts": request.app.state.layouts.previews_for_source(source.id),
                "entry_count": len(request.app.state.processing.entries_for_source(source.id)),
            }
            for source in sources
        ]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="job.html",
            context={
                "job": job,
                "sources": source_items,
                "exports_available": request.app.state.exports.available(job_id),
                "processing_active": any(
                    source.processing_status in {"queued", "running"} for source in sources
                ),
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
        request.app.state.exports.invalidate(job_id)
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
        request.app.state.exports.invalidate(job_id)
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
        request.app.state.exports.invalidate(job_id)
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
        request.app.state.exports.invalidate(job_id)
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/layout-previews/{preview_id}/exclusion")
    def exclude_layout_preview(
        request: Request,
        job_id: str,
        preview_id: str,
    ) -> RedirectResponse:
        try:
            request.app.state.layouts.exclude_preview(
                job_id=job_id,
                preview_id=preview_id,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        request.app.state.exports.invalidate(job_id)
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/sources/{source_id}/processing")
    def process_source(
        request: Request,
        background_tasks: BackgroundTasks,
        job_id: str,
        source_id: str,
    ) -> RedirectResponse:
        try:
            request.app.state.processing.queue_source(job_id=job_id, source_id=source_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        request.app.state.exports.invalidate(job_id)
        background_tasks.add_task(
            request.app.state.processing_queue.run,
            lambda: request.app.state.processing.run_queued_source(
                job_id=job_id,
                source_id=source_id,
            ),
        )
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}/review", response_class=HTMLResponse)
    def review_queue(request: Request, job_id: str, entry_id: str | None = None) -> HTMLResponse:
        try:
            queue = request.app.state.reviews.queue(job_id=job_id, entry_id=entry_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        layout_preview = None
        if queue.current is not None:
            layout_preview = next(
                (
                    layout
                    for layout in request.app.state.layouts.previews_for_source(
                        queue.current.source_id
                    )
                    if layout.page_number == queue.current.page_number
                ),
                None,
            )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="review.html",
            context={
                "job_id": job_id,
                "queue": queue,
                "entry": queue.current,
                "previous": queue.previous,
                "layout_preview": layout_preview,
                "effective_elector_number": effective_elector_number,
                "effective_register_status": effective_register_status,
                "status_label": status_label,
                "warning_label": warning_label,
            },
        )

    @app.post("/jobs/{job_id}/entries/{entry_id}/review")
    def review_entry(
        request: Request,
        job_id: str,
        entry_id: str,
        action: Annotated[str, Form()],
        elector_number: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            request.app.state.reviews.apply(
                job_id=job_id,
                entry_id=entry_id,
                action=action,
                elector_number=elector_number,
                note=note,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        request.app.state.exports.invalidate(job_id)
        return RedirectResponse(
            url=f"/jobs/{job_id}/review",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/entries/{entry_id}/manual-entry")
    def add_manual_entry(
        request: Request,
        job_id: str,
        entry_id: str,
        elector_number: Annotated[str, Form()],
        register_status: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            entry = request.app.state.reviews.add_manual_entry(
                job_id=job_id,
                after_entry_id=entry_id,
                elector_number=elector_number,
                register_status=register_status,
                note=note,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        request.app.state.exports.invalidate(job_id)
        return RedirectResponse(
            url=f"/jobs/{job_id}/review?entry_id={entry.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}/entries/{entry_id}/entry-crop")
    def entry_crop(request: Request, job_id: str, entry_id: str) -> FileResponse:
        try:
            path = request.app.state.reviews.crop_path(job_id=job_id, entry_id=entry_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return FileResponse(path, media_type="image/png")

    @app.post("/jobs/{job_id}/exports")
    def create_exports(request: Request, job_id: str) -> RedirectResponse:
        try:
            request.app.state.exports.generate(job_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ExportNotReadyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return RedirectResponse(
            url=f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}/exports/{export_name}")
    def download_export(
        request: Request,
        job_id: str,
        export_name: Literal["all-electors", "voted-only", "manifest"],
    ) -> FileResponse:
        try:
            path = request.app.state.exports.download_path(
                job_id=job_id,
                export_name=export_name,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        media_type = "application/json" if path.suffix == ".json" else "text/csv"
        return FileResponse(path, media_type=media_type, filename=path.name)

    return app
