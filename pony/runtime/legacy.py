"""Fail-closed preflight for retired Sandbox session bindings."""

from pathlib import Path

from pony.state.legacy_artifacts import LegacyArtifactError, legacy_sandbox_session_bound


class LegacySandboxResumeError(ValueError):
    def __init__(self, code, *, reason_code=""):
        self.code = str(code)
        self.reason_code = str(reason_code or self.code)
        super().__init__(self.code)


def preflight_legacy_sandbox_resume(workspace_root, session_id):
    """Reject an old Sandbox-bound Session before any resume-side write."""
    root = Path(workspace_root)
    try:
        bound = legacy_sandbox_session_bound(root, session_id)
    except LegacyArtifactError as exc:
        raise LegacySandboxResumeError(
            "sandbox_state_invalid", reason_code=exc.code
        ) from exc
    if bound:
        raise LegacySandboxResumeError("legacy_sandbox_session_unsupported")
