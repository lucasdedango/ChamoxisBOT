import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Service(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    name: str
    type: Literal["process", "windows_service", "http", "python"] = "http"
    desired: Literal["running", "stopped", "monitor"] = "monitor"
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None
    windows_name: str | None = None
    health_url: str | None = None
    health_key_env: str | None = None
    health_model: str | None = None
    interval: float = Field(default=30, ge=1)
    startup_delay: float = Field(default=30, ge=0)
    timeout: float = Field(default=5, gt=0)
    restart: bool = False
    max_attempts: int = Field(default=3, ge=0, le=20)
    backoff: float = Field(default=10, ge=1)
    external_supervisor: bool = True
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_start(self):
        if self.restart and self.external_supervisor:
            raise ValueError("Explicitly disable external_supervisor before enabling restarts")
        if self.type in {"process", "python"} and self.desired == "running" and not self.command:
            raise ValueError("Managed processes need a command")
        if self.type == "windows_service" and not self.windows_name:
            raise ValueError("windows_name is required")
        if self.type == "http" and self.restart:
            raise ValueError("Remote HTTP endpoints can only be monitored")
        return self


class Registry:
    def __init__(self, services=()):
        entries = list(services)
        self.services = {s.id: s for s in entries}
        if len(entries) != len(self.services):
            raise ValueError("Duplicate service IDs")
        for service in entries:
            if any(dep not in self.services for dep in service.depends_on):
                raise ValueError(f"Unknown dependency for {service.id}")
        def visit(key, seen):
            if key in seen:
                raise ValueError("Service dependency cycle")
            for dep in self.services[key].depends_on:
                visit(dep, seen | {key})
        for key in self.services:
            visit(key, set())

    @classmethod
    def load(cls, path):
        contents = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(Service.model_validate(item) for item in contents["services"])
