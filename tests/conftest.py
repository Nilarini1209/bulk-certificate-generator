import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=str(tmp_path / "storage"),
        max_recipients=50,
    )
    app = create_app(settings, resume_on_startup=False)
    # TestClient runs FastAPI BackgroundTasks before returning the response,
    # so job processing is deterministic in tests.
    return TestClient(app)


def make_payload(recipients, **overrides):
    payload = {"course_name": "Python 101", "issuer": "Acme Academy", "recipients": recipients}
    payload.update(overrides)
    return payload
