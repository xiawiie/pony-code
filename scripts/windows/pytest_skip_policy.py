"""Fail the Windows release gate on unapproved pytest skip reasons."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import os

import pytest


APPROVED_WINDOWS_SKIP_REASONS = frozenset(
    {
        "cross-process file locks unavailable",
        "fcntl unavailable",
        "FIFO unavailable",
        "FIFO unsupported",
        "fork unavailable",
        "no /dev/null",
        "platform does not expose fcntl locks",
        "POSIX detached process contract",
        "POSIX dir_fd inode race contract",
        "POSIX executable trust contract",
        "POSIX fcntl availability contract",
        "POSIX filename grammar",
        "POSIX mode assertion",
        "POSIX mode preservation contract",
        "POSIX modes required",
        "POSIX owner-mode contract",
        "POSIX parent mode contract",
        "POSIX permission assertion",
        "POSIX shell gate contract",
        "real FIFO probe requires POSIX FIFO support",
        "requires POSIX descriptor traversal",
        "SIGTERM unavailable",
        (
            "Windows symlink attack fixture requires Developer Mode or "
            "SeCreateSymbolicLinkPrivilege"
        ),
        (
            "Windows symlink creation requires Developer Mode or "
            "SeCreateSymbolicLinkPrivilege"
        ),
    }
)
_MAX_UNKNOWN_NODES = 20


def _skip_reason(report) -> str:
    if getattr(report, "wasxfail", None):
        return f"xfail: {report.wasxfail}"
    detail = report.longrepr
    if isinstance(detail, tuple) and len(detail) == 3:
        detail = detail[2]
    reason = str(detail).strip()
    prefix = "Skipped: "
    if reason.startswith(prefix):
        reason = reason[len(prefix) :].strip()
    return reason or "<missing skip reason>"


class WindowsSkipPolicy:
    """Collect Windows skips/xfails and reject reasons outside the audited set."""

    def __init__(self) -> None:
        self.skip_counts: Counter[str] = Counter()
        self.unknown_nodes: dict[str, list[str]] = defaultdict(list)

    def pytest_runtest_logreport(self, report) -> None:
        if not report.skipped and not getattr(report, "wasxfail", None):
            return
        reason = _skip_reason(report)
        self.skip_counts[reason] += 1
        if reason not in APPROVED_WINDOWS_SKIP_REASONS:
            self.unknown_nodes[reason].append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        del exitstatus
        if self.unknown_nodes:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        terminalreporter.write_sep("=", "Windows platform skip audit")
        if not self.skip_counts:
            terminalreporter.write_line("no skips")
        else:
            for reason, count in sorted(self.skip_counts.items()):
                terminalreporter.write_line(f"{count}: {reason}")
        for reason, nodes in sorted(self.unknown_nodes.items()):
            terminalreporter.write_line(f"unapproved: {reason}")
            for nodeid in nodes[:_MAX_UNKNOWN_NODES]:
                terminalreporter.write_line(f"  {nodeid}")
            omitted = len(nodes) - _MAX_UNKNOWN_NODES
            if omitted > 0:
                terminalreporter.write_line(f"  ... {omitted} more")
        payload = {
            "approved": not self.unknown_nodes,
            "counts": dict(sorted(self.skip_counts.items())),
            "schema_version": 1,
            "unknown_reasons": sorted(self.unknown_nodes),
        }
        terminalreporter.write_line(
            "windows_skip_audit="
            + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        )


def pytest_configure(config) -> None:
    if os.name != "nt":
        raise pytest.UsageError("Windows skip policy can only run on Windows")
    config.pluginmanager.register(WindowsSkipPolicy(), "pony-windows-skip-policy")
