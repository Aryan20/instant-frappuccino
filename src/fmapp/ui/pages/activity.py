"""Activity — every job, its steps and full output."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.jobs import JobRun, RunState
from fmapp.ui.widgets import LogView, Pill, button, empty_state, label, page_header, splitter

ICONS = {
    RunState.QUEUED: "◌",
    RunState.RUNNING: "◐",
    RunState.SUCCEEDED: "✔",
    RunState.FAILED: "✖",
    RunState.CANCELLED: "⏹",
}


def _ago(ts: float) -> str:
    delta = int(time.time() - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return time.strftime("%H:%M", time.localtime(ts))


class ActivityPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.run: JobRun | None = None
        box = QVBoxLayout(self)
        box.setContentsMargins(*theme.PAGE_MARGINS)
        box.setSpacing(theme.SECTION_GAP)
        header, _ = page_header("Activity", "Everything run on your behalf, with full output")
        box.addWidget(header)

        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda cur, _p: self._select(cur))

        right = QWidget()
        rbox = QVBoxLayout(right)
        rbox.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.title = label("No activity yet", "h2")
        self.state = Pill()
        self.state.hide()
        top.addWidget(self.title)
        top.addWidget(self.state)
        top.addStretch()
        self.copy_btn = button("Copy log", on_click=self._copy)
        self.retry_btn = button("Run again", on_click=self._retry)
        self.cancel_btn = button("Cancel", "danger", on_click=self._cancel)
        for widget in (self.copy_btn, self.retry_btn, self.cancel_btn):
            top.addWidget(widget)
        rbox.addLayout(top)
        self.steps = label("", "muted", wrap=True)
        rbox.addWidget(self.steps)
        self.log = LogView()
        rbox.addWidget(self.log, 1)
        self.split = splitter(self.list, right, sizes=(320, 720))
        box.addWidget(self.split, 1)
        self.empty = empty_state(
            "No activity yet",
            "Everything you start — creating sites, restarts, builds, deploys — shows up here "
            "with its live output, so you can follow along or copy the log.",
            button("Create a site", "primary", on_click=lambda: ctx.new_site.emit([])),
        )
        box.addWidget(self.empty, 1)
        self._buttons()
        self._sync_empty()

        ctx.jobs.added.connect(lambda _r: self._render_list())
        ctx.jobs.changed.connect(self._refresh)

    def show_run(self, run: JobRun) -> None:
        self._render_list()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == run.id:
                self.list.setCurrentRow(i)
                return

    def _render_list(self) -> None:
        current = self.run.id if self.run else None
        self.list.blockSignals(True)
        self.list.clear()
        for run in self.ctx.jobs.runs:
            item = QListWidgetItem(f"{ICONS[run.state]}  {run.job.title}\n     {_ago(run.created)}")
            item.setData(Qt.ItemDataRole.UserRole, run.id)
            self.list.addItem(item)
            if run.id == current:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if self.run is None and self.ctx.jobs.runs:
            self.list.setCurrentRow(0)
        self._sync_empty()

    def _sync_empty(self) -> None:
        has_runs = bool(self.ctx.jobs.runs)
        self.split.setVisible(has_runs)
        self.empty.setVisible(not has_runs)

    def _refresh(self) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            run = self._find(item.data(Qt.ItemDataRole.UserRole))
            if run:
                item.setText(f"{ICONS[run.state]}  {run.job.title}\n     {_ago(run.created)}")
        self._header()

    def _find(self, run_id: int) -> JobRun | None:
        return next((r for r in self.ctx.jobs.runs if r.id == run_id), None)

    def _select(self, item: QListWidgetItem | None) -> None:
        if self.run:
            self.run.output.disconnect(self.log.append_text)
        self.run = self._find(item.data(Qt.ItemDataRole.UserRole)) if item else None
        self.log.clear()
        if self.run:
            self.log.append_text(self.run.text())
            self.run.output.connect(self.log.append_text)
        self._header()

    def _header(self) -> None:
        run = self.run
        if not run:
            self.title.setText("No activity yet")
            self.state.hide()
            self.steps.setText("")
            self._buttons()
            return
        self.title.setText(run.job.title)
        self.state.set(run.state.value)
        self.state.show()
        parts = []
        for i, step in enumerate(run.job.steps):
            if i < run.step_index or (i == run.step_index and run.state is RunState.SUCCEEDED):
                mark = "✔"
            elif i == run.step_index:
                mark = {RunState.RUNNING: "◐", RunState.FAILED: "✖"}.get(run.state, "·")
            else:
                mark = "·"
            parts.append(f"{mark} {step.title}")
        self.steps.setText("    ".join(parts))
        self._buttons()

    def _buttons(self) -> None:
        run = self.run
        self.cancel_btn.setVisible(bool(run and not run.done))
        self.retry_btn.setVisible(bool(run and run.state in (RunState.FAILED, RunState.CANCELLED)))
        self.copy_btn.setEnabled(run is not None)

    def _cancel(self) -> None:
        if self.run:
            self.run.cancel()

    def _retry(self) -> None:
        if self.run:
            self.show_run(self.ctx.jobs.retry(self.run))

    def _copy(self) -> None:
        if self.run:
            QApplication.clipboard().setText(self.run.text())
