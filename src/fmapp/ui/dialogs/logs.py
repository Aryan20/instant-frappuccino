"""A live log window for any streaming job (container logs, service logs)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

from fmapp.core.operations import Job
from fmapp.ui.jobs import StreamProcess
from fmapp.ui.widgets import LogView, button, label


class LogDialog(QDialog):
    def __init__(self, title: str, job: Job, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # non-modal: free it when closed
        self.resize(960, 560)
        box = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(label(title, "h2"), 1)
        self.state = label("following…", "muted")
        row.addWidget(self.state)
        row.addWidget(button("Clear", on_click=lambda: self.view.clear()))
        row.addWidget(button("Close", on_click=self.close))
        box.addLayout(row)
        self.view = LogView()
        box.addWidget(self.view, 1)
        self.stream = StreamProcess(self)
        self.stream.output.connect(self.view.append_text)
        self.stream.stopped.connect(lambda: self.state.setText("stopped"))
        self.stream.start(job)

    def done(self, result: int) -> None:
        self.stream.stop()
        super().done(result)

    def closeEvent(self, event) -> None:
        self.stream.stop()
        super().closeEvent(event)


def show_logs(parent, title: str, job: Job) -> None:
    dialog = LogDialog(title, job, parent)
    dialog.setModal(False)
    dialog.show()
