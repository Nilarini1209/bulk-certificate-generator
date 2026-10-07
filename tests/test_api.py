import io
import zipfile

import pytest
from pypdf import PdfReader

from app import pdf, services
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

from .conftest import make_payload

URL = "/api/v1/jobs"


def pdf_text(content: bytes) -> str:
    return "".join(p.extract_text() for p in PdfReader(io.BytesIO(content)).pages)


# ----------------------------- creating a job ----------------------------- #
def test_create_job_returns_202_and_job_info(client):
    r = client.post(URL, json=make_payload([{"name": "Alice", "email": "alice@example.com"}]))
    assert r.status_code == 202
    body = r.json()
    assert body["id"] and body["total"] == 1
    assert body["links"]["self"] == f"/api/v1/jobs/{body['id']}"


def test_bulk_request_creates_one_certificate_per_recipient(client):
    recipients = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(40)]
    job_id = client.post(URL, json=make_payload(recipients)).json()["id"]
    job = client.get(f"{URL}/{job_id}").json()
    assert job["status"] == "completed"
    assert job["total"] == job["succeeded"] == 40


# ------------------------------- validation ------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"recipients": [{"name": "A"}]},                         # missing course_name
        make_payload([]),                                        # empty list
        make_payload([{"name": "A"}], course_name=""),           # blank course
        make_payload([{"name": "A"}], issued_on="not-a-date"),   # bad date
        make_payload([{"name": f"P{i}"} for i in range(51)]),    # over max_recipients (50)
    ],
)
def test_invalid_request_is_rejected(client, payload):
    assert client.post(URL, json=payload).status_code == 422


def test_invalid_recipients_are_recorded_not_fatal(client):
    recipients = [
        {"name": "Good One", "email": "good@example.com"},
        {"name": "", "email": "blank@example.com"},          # blank name
        {"email": "noname@example.com"},                      # missing name
        {"name": "Bad Email", "email": "not-an-email"},       # bad email
        "just a string",                                      # wrong type
        {"name": "Dup", "email": "GOOD@example.com"},         # duplicate email
        {"name": "No Email Needed"},                          # email is optional
    ]
    job_id = client.post(URL, json=make_payload(recipients)).json()["id"]
    job = client.get(f"{URL}/{job_id}").json()
    assert (job["total"], job["succeeded"], job["failed"]) == (7, 2, 5)
    assert job["status"] == "completed_with_errors"

    failed = client.get(f"{URL}/{job_id}/certificates", params={"status": "failed"}).json()
    assert failed["total"] == 5
    assert {i["row_index"] for i in failed["items"]} == {1, 2, 3, 4, 5}
    assert all(i["error"] and i["download_url"] is None for i in failed["items"])


def test_all_invalid_recipients_marks_job_failed(client):
    job_id = client.post(URL, json=make_payload([{"name": ""}, {"foo": "bar"}])).json()["id"]
    job = client.get(f"{URL}/{job_id}").json()
    assert job["status"] == "failed" and job["failed"] == 2 and job["completed_at"]


# ---------------------------- certificate content --------------------------- #
def test_pdf_contains_recipient_specific_data(client):
    job_id = client.post(
        URL,
        json=make_payload(
            [{"name": "Grace Hopper"}], issued_on="2025-03-14", title="Certificate of Excellence"
        ),
    ).json()["id"]
    item = client.get(f"{URL}/{job_id}/certificates").json()["items"][0]
    r = client.get(item["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    text = pdf_text(r.content)
    for expected in ["Grace Hopper", "Python 101", "Certificate of Excellence", "14 March 2025",
                     "Acme Academy", item["id"]]:
        assert expected in text


def test_render_handles_very_long_names():
    data = pdf.CertificateData("id1", "X" * 120, "Course", "Title", __import__("datetime").date.today())
    assert pdf.render_certificate_pdf(data).startswith(b"%PDF")


# ------------------------------ status / progress --------------------------- #
def test_job_status_not_found(client):
    assert client.get(f"{URL}/does-not-exist").status_code == 404


def test_progress_while_job_is_partially_processed(client):
    """Create the job without running the worker, then process in two steps."""
    app = client.app
    sf = app.state.session_factory
    from app.schemas import JobCreate

    with sf() as db:
        job = services.create_job(
            db, JobCreate(course_name="C", recipients=[{"name": "A"}, {"name": "B"}, {"name": ""}])
        )
        job_id = job.id
    job = client.get(f"{URL}/{job_id}").json()
    assert job["status"] == "pending"
    assert (job["pending"], job["failed"], job["succeeded"]) == (2, 1, 0)
    assert job["progress_percent"] == pytest.approx(33.3)

    services.process_job(sf, app.state.settings.storage_dir, job_id)
    job = client.get(f"{URL}/{job_id}").json()
    assert job["status"] == "completed_with_errors"
    assert job["progress_percent"] == 100.0 and job["pending"] == 0


# --------------------- individual generation failure ----------------------- #
def test_one_generation_failure_does_not_stop_the_others(client, monkeypatch):
    real = pdf.render_certificate_pdf

    def flaky(data):
        if data.recipient_name == "Boom":
            raise RuntimeError("renderer exploded")
        return real(data)

    monkeypatch.setattr(pdf, "render_certificate_pdf", flaky)
    recipients = [{"name": "Ok 1"}, {"name": "Boom"}, {"name": "Ok 2"}, {"name": "Ok 3"}]
    job_id = client.post(URL, json=make_payload(recipients)).json()["id"]

    job = client.get(f"{URL}/{job_id}").json()
    assert (job["succeeded"], job["failed"], job["status"]) == (3, 1, "completed_with_errors")

    failed = client.get(f"{URL}/{job_id}/certificates", params={"status": "failed"}).json()["items"]
    assert len(failed) == 1
    assert failed[0]["recipient_name"] == "Boom" and "renderer exploded" in failed[0]["error"]
    # the failed one cannot be downloaded
    assert failed[0]["download_url"] is None


def test_all_generation_failures_mark_job_failed(client, monkeypatch):
    monkeypatch.setattr(pdf, "render_certificate_pdf", lambda d: (_ for _ in ()).throw(OSError("x")))
    job_id = client.post(URL, json=make_payload([{"name": "A"}, {"name": "B"}])).json()["id"]
    assert client.get(f"{URL}/{job_id}").json()["status"] == "failed"


def test_interrupted_job_resumes_only_pending_certificates(client, monkeypatch):
    sf, storage = client.app.state.session_factory, client.app.state.settings.storage_dir
    from app.schemas import JobCreate

    with sf() as db:
        job_id = services.create_job(
            db, JobCreate(course_name="C", recipients=[{"name": "A"}, {"name": "B"}])
        ).id
    calls = []
    real = pdf.render_certificate_pdf
    monkeypatch.setattr(pdf, "render_certificate_pdf", lambda d: (calls.append(d.recipient_name), real(d))[1])
    services.resume_unfinished_jobs(sf, storage)
    services.resume_unfinished_jobs(sf, storage)  # second run finds nothing to do
    assert sorted(calls) == ["A", "B"]
    assert client.get(f"{URL}/{job_id}").json()["status"] == "completed"


# ------------------------------- retrieval --------------------------------- #
def test_list_certificates_pagination_and_filter(client):
    recipients = [{"name": f"P{i}"} for i in range(10)] + [{"name": ""}]
    job_id = client.post(URL, json=make_payload(recipients)).json()["id"]
    page = client.get(f"{URL}/{job_id}/certificates", params={"limit": 4, "offset": 8}).json()
    assert page["total"] == 11 and len(page["items"]) == 3
    assert [i["row_index"] for i in page["items"]] == [8, 9, 10]
    ok = client.get(f"{URL}/{job_id}/certificates", params={"status": "success"}).json()
    assert ok["total"] == 10
    assert client.get(f"{URL}/{job_id}/certificates", params={"status": "bogus"}).status_code == 422


def test_download_unknown_or_failed_certificate(client):
    assert client.get("/api/v1/certificates/nope/download").status_code == 404
    job_id = client.post(URL, json=make_payload([{"name": ""}, {"name": "Fine"}])).json()["id"]
    items = client.get(f"{URL}/{job_id}/certificates").json()["items"]
    failed = next(i for i in items if i["status"] == "failed")
    assert client.get(f"/api/v1/certificates/{failed['id']}/download").status_code == 409


def test_download_zip_contains_only_successful_certificates(client):
    recipients = [{"name": "Ada Lovelace"}, {"name": ""}, {"name": "Alan Turing"}]
    job_id = client.post(URL, json=make_payload(recipients)).json()["id"]
    r = client.get(f"{URL}/{job_id}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == ["0001_Ada_Lovelace.pdf", "0003_Alan_Turing.pdf"]
    assert "Alan Turing" in pdf_text(zf.read("0003_Alan_Turing.pdf"))


def test_zip_when_nothing_generated_is_409(client):
    job_id = client.post(URL, json=make_payload([{"name": ""}])).json()["id"]
    assert client.get(f"{URL}/{job_id}/download").status_code == 409


def test_missing_file_on_disk_returns_410(client):
    job_id = client.post(URL, json=make_payload([{"name": "Gone"}])).json()["id"]
    item = client.get(f"{URL}/{job_id}/certificates").json()["items"][0]
    import pathlib
    from app.models import Certificate
    with client.app.state.session_factory() as db:
        pathlib.Path(db.get(Certificate, item["id"]).file_path).unlink()
    assert client.get(item["download_url"]).status_code == 410
