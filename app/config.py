import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./certgen.db")
    )
    storage_dir: str = field(
        default_factory=lambda: os.getenv("STORAGE_DIR", "./storage")
    )
    # Hard cap on recipients per request (protects memory / request size).
    max_recipients: int = field(
        default_factory=lambda: int(os.getenv("MAX_RECIPIENTS", "5000"))
    )
