import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import pdf
from .models import Certificate, CertStatus, Job, JobStatus
from .schemas import JobCreate, RecipientIn

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'recipient'}: {e['msg']}" for e in exc.errors()
    )


# --------------------------------------------------------------------------- #
# Job creation (fast, synchronous: validate + persist, no PDF work)
# --------------------------------------------------------------------------- #
def create_job(db: Session, payload: JobCreate) -> Job:
    job = Job(
        course_name=payload.course_name.strip(),
        title=payload.title.strip(),
        issuer=payload.issuer.strip() if payload.issuer else None,
        issued_on=payload.issued_on,
    )
    db.add(job)

    seen_emails: set[str] = set()
    for idx, raw in enumerate(payload.recipients):
        cert = Certificate(row_index=idx)
        job.certificates.append(cert)
        raw_dict = raw if isinstance(raw, dict) else {"value": repr(raw)[:200]}
        cert.raw_input = raw_dict

        try:
            if not isinstance(raw, dict):
                raise ValueError("recipient must be an object with a 'name' field")
            recipient = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            cert.status, cert.error = CertStatus.FAILED.value, _format_validation_error(exc)
            cert.recipient_name = str(raw.get("name"))[:200] if raw.get("name") else None
            cert.recipient_email = str(raw.get("email"))[:320] if raw.get("email") else None
            continue
        except ValueError as exc:
            cert.status, cert.error = CertStatus.FAILED.value, str(exc)
            continue

        cert.recipient_name = recipient.name
        cert.recipient_email = recipient.email
        if recipient.email:
            key = recipient.email.lower()
            if key in seen_emails:
                cert.status = CertStatus.FAILED.value
                cert.error = f"duplicate recipient email in this job: {recipient.email}"
                continue
            seen_emails.add(key)
        cert.status = CertStatus.PENDING.value

    db.flush()
    if not any(c.status == CertStatus.PENDING.value for c in job.certificates):
        # Nothing to generate: finalize immediately.
        job.status = JobStatus.FAILED.value
        job.completed_at = _now()
    db.commit()
    return job


# --------------------------------------------------------------------------- #
# Background processing
# --------------------------------------------------------------------------- #
def _cert_path(storage_dir: str, job_id: str, cert_id: str) -> Path:
    return Path(storage_dir) / job_id / f"{cert_id}.pdf"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # never leaves a half-written PDF at the final path


def process_job(session_factory: sessionmaker, storage_dir: str, job_id: str) -> None:
    """Generate every pending certificate of a job.

    Each certificate is isolated in its own try/except and committed on its own, so
    (a) one failure never affects the others and (b) progress is visible while running
    and survives a crash (re-running only picks up what's still 'pending').
    """
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.PROCESSING.value
        db.commit()

        pending = db.scalars(
            select(Certificate)
            .where(Certificate.job_id == job_id, Certificate.status == CertStatus.PENDING.value)
            .order_by(Certificate.row_index)
        ).all()

        for cert in pending:
            try:
                data = pdf.CertificateData(
                    certificate_id=cert.id,
                    recipient_name=cert.recipient_name,
                    course_name=job.course_name,
                    title=job.title,
                    issued_on=job.issued_on,
                    issuer=job.issuer,
                )
                content = pdf.render_certificate_pdf(data)
                path = _cert_path(storage_dir, job_id, cert.id)
                _write_atomic(path, content)
                cert.file_path = str(path)
                cert.status = CertStatus.SUCCESS.value
                cert.error = None
            except Exception as exc:  # noqa: BLE001 - isolate *any* per-item failure
                log.exception("certificate %s failed", cert.id)
                cert.status = CertStatus.FAILED.value
                cert.error = f"generation failed: {type(exc).__name__}: {exc}"
            cert.completed_at = _now()
            db.commit()

        finalize_job(db, job)


def finalize_job(db: Session, job: Job) -> None:
    counts = get_counts(db, job.id)
    if counts["pending"]:
        return
    if counts["failed"] == 0:
        job.status = JobStatus.COMPLETED.value
    elif counts["succeeded"] == 0:
        job.status = JobStatus.FAILED.value
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS.value
    job.completed_at = _now()
    db.commit()


def resume_unfinished_jobs(session_factory: sessionmaker, storage_dir: str) -> None:
    """Called on startup: jobs interrupted by a restart continue where they stopped."""
    with session_factory() as db:
        ids = db.scalars(
            select(Job.id).where(
                Job.status.in_([JobStatus.PENDING.value, JobStatus.PROCESSING.value])
            )
        ).all()
    for job_id in ids:
        process_job(session_factory, storage_dir, job_id)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def get_counts(db: Session, job_id: str) -> dict[str, int]:
    rows = db.execute(
        select(Certificate.status, func.count())
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()
    by_status = {status: n for status, n in rows}
    succeeded = by_status.get(CertStatus.SUCCESS.value, 0)
    failed = by_status.get(CertStatus.FAILED.value, 0)
    pending = by_status.get(CertStatus.PENDING.value, 0)
    return {
        "total": succeeded + failed + pending,
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
    }


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(row_index: int, name: str | None) -> str:
    stem = _SAFE.sub("_", name or "recipient").strip("_") or "recipient"
    return f"{row_index + 1:04d}_{stem}.pdf"
