import io
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .config import Settings
from .database import Base, make_engine, make_session_factory
from .models import Certificate, CertStatus, Job
from .schemas import CertificateList, CertificateOut, JobCreate, JobOut


def create_app(settings: Settings | None = None, resume_on_startup: bool = True) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    Base.metadata.create_all(engine)
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if resume_on_startup:
            threading.Thread(
                target=services.resume_unfinished_jobs,
                args=(session_factory, settings.storage_dir),
                daemon=True,
            ).start()
        yield

    app = FastAPI(title="Bulk Certificate Generator", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory

    def get_db(request: Request):
        with request.app.state.session_factory() as db:
            yield db

    # -- helpers ----------------------------------------------------------- #
    def job_out(db: Session, job: Job) -> JobOut:
        c = services.get_counts(db, job.id)
        done = c["succeeded"] + c["failed"]
        return JobOut(
            id=job.id,
            status=job.status,
            course_name=job.course_name,
            created_at=job.created_at,
            completed_at=job.completed_at,
            progress_percent=round(100 * done / c["total"], 1) if c["total"] else 100.0,
            links={
                "self": f"/api/v1/jobs/{job.id}",
                "certificates": f"/api/v1/jobs/{job.id}/certificates",
                "download_all": f"/api/v1/jobs/{job.id}/download",
            },
            **c,
        )

    def cert_out(c: Certificate) -> CertificateOut:
        return CertificateOut(
            id=c.id,
            row_index=c.row_index,
            recipient_name=c.recipient_name,
            recipient_email=c.recipient_email,
            status=c.status,
            error=c.error,
            download_url=f"/api/v1/certificates/{c.id}/download"
            if c.status == CertStatus.SUCCESS.value
            else None,
        )

    def get_job_or_404(db: Session, job_id: str) -> Job:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    # -- endpoints --------------------------------------------------------- #
    @app.post("/api/v1/jobs", response_model=JobOut, status_code=202)
    def create_job(
        payload: JobCreate,
        background: BackgroundTasks,
        request: Request,
        db: Session = Depends(get_db),
    ):
        if len(payload.recipients) > settings.max_recipients:
            raise HTTPException(
                422, f"Too many recipients (max {settings.max_recipients} per request)"
            )
        job = services.create_job(db, payload)
        if job.status != "failed":  # failed => nothing to generate
            background.add_task(
                services.process_job,
                request.app.state.session_factory,
                settings.storage_dir,
                job.id,
            )
        return job_out(db, job)

    @app.get("/api/v1/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: str, db: Session = Depends(get_db)):
        return job_out(db, get_job_or_404(db, job_id))

    @app.get("/api/v1/jobs/{job_id}/certificates", response_model=CertificateList)
    def list_certificates(
        job_id: str,
        status: CertStatus | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
    ):
        get_job_or_404(db, job_id)
        q = select(Certificate).where(Certificate.job_id == job_id)
        if status:
            q = q.where(Certificate.status == status.value)
        total = db.scalar(select(func.count()).select_from(q.subquery()))
        rows = db.scalars(q.order_by(Certificate.row_index).limit(limit).offset(offset)).all()
        return CertificateList(
            total=total, limit=limit, offset=offset, items=[cert_out(c) for c in rows]
        )

    @app.get("/api/v1/certificates/{cert_id}/download")
    def download_certificate(cert_id: str, db: Session = Depends(get_db)):
        cert = db.get(Certificate, cert_id)
        if not cert:
            raise HTTPException(404, "Certificate not found")
        if cert.status != CertStatus.SUCCESS.value or not cert.file_path:
            raise HTTPException(409, f"Certificate is not available (status: {cert.status})")
        if not Path(cert.file_path).exists():
            raise HTTPException(410, "Certificate file is missing from storage")
        return FileResponse(
            cert.file_path,
            media_type="application/pdf",
            filename=services.safe_filename(cert.row_index, cert.recipient_name),
        )

    @app.get("/api/v1/jobs/{job_id}/download")
    def download_job_zip(job_id: str, db: Session = Depends(get_db)):
        job = get_job_or_404(db, job_id)
        certs = db.scalars(
            select(Certificate)
            .where(Certificate.job_id == job_id, Certificate.status == CertStatus.SUCCESS.value)
            .order_by(Certificate.row_index)
        ).all()
        if not certs:
            raise HTTPException(409, "No certificates have been generated yet")
        # PDFs are already compressed, so ZIP_STORED avoids wasted CPU.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for c in certs:
                if c.file_path and Path(c.file_path).exists():
                    zf.write(c.file_path, services.safe_filename(c.row_index, c.recipient_name))
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="certificates_{job.id}.zip"'},
        )

    return app

