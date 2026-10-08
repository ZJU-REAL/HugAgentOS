"""Application hosting settings loaded through the platform environment reader."""

from dataclasses import dataclass, field

from core.config.settings import _env


@dataclass(frozen=True)
class ApplicationHostingSettings:
    database_url: str = field(default_factory=lambda: _env("APPLICATION_DATABASE_URL", "").strip())


application_hosting_settings = ApplicationHostingSettings()
