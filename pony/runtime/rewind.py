"""Workspace rewind domain errors and result values."""

from dataclasses import dataclass
import os


class WorkspaceRewindError(RuntimeError):
    code = "workspace_rewind_failed"


class WorkspaceRewindConfirmationRequired(WorkspaceRewindError):
    code = "workspace_rewind_confirmation_required"

    def __init__(self, preview):
        super().__init__(
            "workspace_rewind_confirmation_required: review the restore plan and confirm once"
        )
        self.preview = preview


@dataclass(frozen=True)
class WorkspaceRewindResult:
    rewind_entry: dict
    summary_entry: dict | None
    restore_result: dict
    preview: dict


def lexical_workspace_root(value):
    return os.path.abspath(os.path.expanduser(str(value)))
