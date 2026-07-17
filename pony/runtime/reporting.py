"""Runtime report projection helpers."""


def build_report_request_metadata(task_state, last_request_metadata):
    """Promote the latest resume status while retaining the prompt-time value."""
    fragment = dict(last_request_metadata)
    if not fragment:
        return fragment
    if task_state.resume_status:
        fragment.setdefault(
            "last_prompt_resume_status",
            fragment.get("resume_status", ""),
        )
        fragment["resume_status"] = task_state.resume_status
    return fragment
