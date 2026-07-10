"""Provider-agnostic response type."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    UNKNOWN = "unknown"


@dataclass
class Response:
    stop_reason: StopReason
    content: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
