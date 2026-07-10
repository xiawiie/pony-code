"""Narrow context passed from runtime into tool functions."""

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    memory_store: Optional[Any] = None
    memory_retrieval: Optional[Any] = None
    repo_map: Optional[Any] = None
    trusted_executables: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.trusted_executables = MappingProxyType(dict(self.trusted_executables))

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()
