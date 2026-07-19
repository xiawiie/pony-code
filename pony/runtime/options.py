"""Immutable optional settings for one Pony runtime instance."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeOptions:
    run_store: Any = None
    project_trusted: bool = False
    max_steps: int = 12
    max_output_tokens: int | None = None
    context_window: int | None = None
    depth: int = 0
    max_depth: int = 1
    read_only: bool = False
    shell_env_allowlist: tuple[str, ...] | None = None
    secret_env_names: tuple[str, ...] | None = None
    redaction_env: dict[str, str] | None = None
    feature_flags: dict[str, bool] | None = None
    allowed_tools: tuple[str, ...] | None = None
    allow_dangerously_skip_permissions: bool = False
    trusted_redaction_env: bool = False
    trusted_executables: dict[str, str] | None = None
    sandbox_context: Any = None
    project_config: dict[str, Any] | None = None
    session_id: str | None = None
    development_runtime_seal: Any = None
