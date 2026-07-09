"""Runtime action types decoded from provider responses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias


class ActionOrigin(str, Enum):
    NATIVE_TOOL_USE = "native_tool_use"
    TEXT_PROTOCOL_TOOL = "text_protocol_tool"
    TEXT_PROTOCOL_FINAL = "text_protocol_final"
    PLAIN_TEXT_FINAL = "plain_text_final"
    MALFORMED_TEXT_PROTOCOL = "malformed_text_protocol"
    EMPTY_RESPONSE = "empty_response"
    STOP_SEQUENCE = "stop_sequence"
    UNSUPPORTED_RESPONSE = "unsupported_response"


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict[str, Any]
    id: str | None
    origin: ActionOrigin
    ignored_tool_count: int = 0


@dataclass(frozen=True)
class FinalAction:
    text: str
    origin: ActionOrigin


@dataclass(frozen=True)
class RetryAction:
    reason: str
    model_visible: bool
    origin: ActionOrigin


AgentAction: TypeAlias = ToolAction | FinalAction | RetryAction
