"""Bounded in-memory input queue for the interactive CLI."""

from collections import deque
from dataclasses import dataclass
import threading


MAX_PENDING_INPUTS = 5


@dataclass(frozen=True)
class SubmitResult:
    status: str
    pending: int


@dataclass
class _Confirmation:
    message: str
    answered: threading.Event
    accepted: bool = False


class InputQueue:
    """Run one input at a time and keep later inputs out of durable state."""

    def __init__(self, process, *, on_start=lambda _text: None, on_wake=lambda: None):
        self._process = process
        self._on_start = on_start
        self._on_wake = on_wake
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending = deque()
        self._busy = False
        self._closing = False
        self._result = None
        self._failure = None
        self._terminal = False
        self._confirmation = None
        self._worker = None

    @property
    def busy(self):
        with self._lock:
            return self._busy

    @property
    def pending_count(self):
        with self._lock:
            return len(self._pending)

    def submit(self, text):
        with self._lock:
            if self._closing or self._terminal:
                return SubmitResult("closed", len(self._pending))
            if self._busy:
                if len(self._pending) == MAX_PENDING_INPUTS:
                    return SubmitResult("full", len(self._pending))
                self._pending.append(text)
                return SubmitResult("queued", len(self._pending))
            self._busy = True
            worker = threading.Thread(
                target=self._run,
                args=(text,),
                name="pony-input-worker",
                daemon=False,
            )
            self._worker = worker
        try:
            worker.start()
        except BaseException:
            with self._lock:
                self._busy = False
                self._worker = None
                self._idle.notify_all()
            raise
        return SubmitResult("started", 0)

    def clear(self):
        with self._lock:
            removed = len(self._pending)
            self._pending.clear()
            return removed

    def confirmation(self):
        with self._lock:
            request = self._confirmation
            return request.message if request is not None else None

    def answer_confirmation(self, answer):
        with self._lock:
            request = self._confirmation
            if request is None:
                return False
            request.accepted = answer.strip().casefold() in {"y", "yes"}
            self._confirmation = None
            request.answered.set()
            return True

    def confirm(self, message):
        request = _Confirmation(message=message, answered=threading.Event())
        with self._lock:
            if self._closing:
                return False
            if self._confirmation is not None:
                return False
            self._confirmation = request
        self._on_wake()
        request.answered.wait()
        return request.accepted

    def terminal_outcome(self):
        with self._lock:
            return self._terminal, self._result, self._failure

    def close(self):
        with self._lock:
            self._closing = True
            self._pending.clear()
            request = self._confirmation
            self._confirmation = None
            if request is not None:
                request.answered.set()
            while self._busy:
                self._idle.wait()

    def _run(self, current):
        while True:
            try:
                self._on_start(current)
                result = self._process(current)
            except BaseException as exc:  # noqa: BLE001 - return failure to UI thread
                self._finish(failure=exc)
                return
            if result is not None:
                self._finish(result=result)
                return
            with self._lock:
                if self._pending and not self._closing:
                    current = self._pending.popleft()
                    continue
                self._busy = False
                self._worker = None
                self._idle.notify_all()
                return

    def _finish(self, *, result=None, failure=None):
        with self._lock:
            self._result = result
            self._failure = failure
            self._terminal = True
            self._pending.clear()
            self._busy = False
            self._worker = None
            self._idle.notify_all()
        self._on_wake()
