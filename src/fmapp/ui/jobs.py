"""Execute :class:`fmapp.core.operations.Job` objects with QProcess.

Jobs touching the same bench run one at a time (fm takes per-bench locks and two
concurrent ``fm`` calls on one bench fight over docker compose); jobs on different
benches run in parallel.
"""

from __future__ import annotations

import codecs
import re
import time
from collections.abc import Callable
from dataclasses import replace
from enum import StrEnum

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from fmapp.core.env import tool_env
from fmapp.core.operations import Job, Step, display_argv
from fmapp.core.textutil import clean_output, mask


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class _Output:
    """Decodes, cleans and masks a process's output stream.

    Decoding is incremental so multi-byte characters split across reads survive. With secrets
    to hide, the last ``len(longest secret) - 1`` characters are held back until the next read:
    that is exactly what a secret split across two reads could span, so it is masked whole
    while everything else (including ``\\r`` progress redraws) still streams live.
    """

    def __init__(self, secrets: list[str], sink: Callable[[str], None]) -> None:
        self.secrets = [s for s in secrets if s]
        self.sink = sink
        self.hold = max((len(s) for s in self.secrets), default=1) - 1
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""

    def feed(self, data: bytes) -> None:
        text = self.pending + self.decoder.decode(data)
        cut = max(0, len(text) - self.hold)
        for secret in self.secrets:  # never cut through a secret that is already complete
            for match in re.finditer(re.escape(secret), text):
                if match.start() < cut < match.end():
                    cut = match.end()
        self.pending = text[cut:]
        self._send(text[:cut])

    def flush(self) -> None:
        text, self.pending = self.pending + self.decoder.decode(b"", final=True), ""
        self._send(text)

    def _send(self, text: str) -> None:
        if text:
            self.sink(mask(clean_output(text), self.secrets))


def _process_env(job: Job) -> QProcessEnvironment:
    env = QProcessEnvironment()
    for key, value in tool_env(job.env).items():
        env.insert(key, value)
    return env


class JobRun(QObject):
    """One execution of a job: state, log and the live process."""

    output = Signal(str)
    changed = Signal()
    finished = Signal()

    _ids = 0

    def __init__(self, job: Job, parent: QObject | None = None) -> None:
        super().__init__(parent)
        JobRun._ids += 1
        self.id = JobRun._ids
        self.job = job
        self.state = RunState.QUEUED
        self.step_index = -1
        self.log: list[str] = []
        self.created = time.time()
        self._process: QProcess | None = None
        self._output: _Output | None = None
        self._cancel_requested = False

    # -- public ---------------------------------------------------------------------------
    @property
    def done(self) -> bool:
        return self.state in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED)

    @property
    def current_step(self) -> Step | None:
        steps = self.job.steps
        return steps[self.step_index] if 0 <= self.step_index < len(steps) else None

    def text(self) -> str:
        return "".join(self.log)

    def start(self) -> None:
        self.state = RunState.RUNNING
        self.changed.emit()
        self._next_step()

    def cancel(self) -> None:
        self._cancel_requested = True
        if self.state is RunState.QUEUED:
            self._finish(RunState.CANCELLED)
        elif self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._emit("\n⏹  Cancelling…\n")
            self._process.terminate()
            QTimer.singleShot(5000, self._kill)

    # -- internals ------------------------------------------------------------------------
    def _kill(self) -> None:
        if self._process and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _emit(self, text: str) -> None:
        text = mask(text, self.job.secrets)
        self.log.append(text)
        self.output.emit(text)

    def _finish(self, state: RunState, error: str = "") -> None:
        self.state = state
        if error:
            self._emit(f"\n✖  {error}\n")
        elif state is RunState.SUCCEEDED:
            self._emit("\n✔  Done\n")
        self.changed.emit()
        self.finished.emit()

    def _next_step(self) -> None:
        if self._cancel_requested:
            self._finish(RunState.CANCELLED)
            return
        self.step_index += 1
        step = self.current_step
        if step is None:
            self._finish(RunState.SUCCEEDED)
            return
        self.changed.emit()
        self._emit(f"\n▶  {step.title}\n")
        try:
            if step.action:
                note = step.action(self.job.context)
                if note:
                    self._emit(note + "\n")
                self._next_step()
                return
        except Exception as exc:
            self._finish(RunState.FAILED, str(exc))
            return
        if not step.argv:
            self._emit("Nothing to do, skipped.\n")
            self._next_step()
            return
        self._spawn(step.argv, step.stdin, step.display())

    def _spawn(self, argv: list[str], stdin: str | None = None, shown: str = "") -> None:
        self._emit("$ " + (shown or display_argv(argv)) + "\n")
        process = QProcess(self)
        process.setProcessEnvironment(_process_env(self.job))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        output = _Output(self.job.secrets, self._emit)
        process.readyReadStandardOutput.connect(lambda: output.feed(bytes(process.readAllStandardOutput())))
        self._output = output
        process.finished.connect(self._on_exit)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        process.start(argv[0], argv[1:])
        if stdin is not None:  # e.g. a deploy config piped to a server over SSH
            process.write(stdin.encode())
        process.closeWriteChannel()

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._finish(
                RunState.FAILED, f"Could not start {self._process.program() if self._process else ''}"
            )

    def _on_exit(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._output:
            self._output.flush()
        if self.done:
            return
        if self._cancel_requested:
            self._finish(RunState.CANCELLED)
        elif status == QProcess.ExitStatus.NormalExit and code == 0:
            self._next_step()
        else:
            step = self.current_step
            self._finish(RunState.FAILED, f"{step.title if step else 'Step'} failed (exit code {code})")


class JobManager(QObject):
    added = Signal(object)  # JobRun
    finished = Signal(object)  # JobRun
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runs: list[JobRun] = []

    def submit(self, job: Job) -> JobRun:
        run = JobRun(job, self)
        run.changed.connect(self.changed)
        run.finished.connect(lambda: self._on_finished(run))
        self.runs.insert(0, run)
        self.added.emit(run)
        self._pump()
        return run

    def retry(self, run: JobRun) -> JobRun:
        return self.submit(replace(run.job, context={}))

    def active(self) -> list[JobRun]:
        return [r for r in self.runs if not r.done]

    def busy_benches(self, host_id: str = "local") -> set[str]:
        """Benches on ``host_id`` that have a running job."""
        return {
            r.job.bench
            for r in self.runs
            if r.state is RunState.RUNNING and r.job.bench and r.job.host.id == host_id
        }

    KEEP_FINISHED = 50  # finished runs (and their logs) kept for the Activity page

    def _prune(self) -> None:
        finished = [r for r in self.runs if r.done]
        for old in finished[self.KEEP_FINISHED :]:
            self.runs.remove(old)
            old.setParent(None)  # let Python free it (and its log) once nothing shows it

    def _on_finished(self, run: JobRun) -> None:
        self._prune()
        self.finished.emit(run)
        self.changed.emit()
        self._pump()

    def _pump(self) -> None:
        # One job at a time per bench *per host*: a server's site may share a local site's name.
        busy = {
            (r.job.host.id, r.job.bench) for r in self.runs if r.state is RunState.RUNNING and r.job.bench
        }
        for run in reversed(self.runs):  # oldest first; runs are stored newest-first
            if run.state is not RunState.QUEUED:
                continue
            key = (run.job.host.id, run.job.bench)
            if run.job.bench and key in busy:
                continue
            if run.job.bench:
                busy.add(key)
            run.start()


class StreamProcess(QObject):
    """A single long-running command whose output is shown live (e.g. ``fm logs -f``)."""

    output = Signal(str)
    stopped = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.state() != QProcess.ProcessState.NotRunning)

    def start(self, job: Job) -> None:
        self.stop()
        argv = job.steps[0].argv or []
        process = QProcess(self)
        process.setProcessEnvironment(_process_env(job))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        output = _Output(job.secrets, self.output.emit)
        process.readyReadStandardOutput.connect(lambda: output.feed(bytes(process.readAllStandardOutput())))
        process.finished.connect(lambda *_: (output.flush(), self.stopped.emit()))
        self._process = process
        process.start(argv[0], argv[1:])

    def stop(self) -> None:
        if self.running and self._process:
            self._process.terminate()
            if not self._process.waitForFinished(3000):
                self._process.kill()
        self._process = None
