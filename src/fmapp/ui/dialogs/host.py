"""Add or edit a server reached over SSH."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from fmapp.core import remote
from fmapp.core.hosts import Host
from fmapp.ui.async_ import run_async
from fmapp.ui.widgets import button, form_layout, label


class HostDialog(QDialog):
    def __init__(self, host: Host | None = None, parent=None) -> None:
        super().__init__(parent)
        self.host = host or Host(name="")
        self.setWindowTitle("Edit server" if host else "Add server")
        self.setMinimumWidth(560)
        box = QVBoxLayout(self)
        box.addWidget(
            label(
                "The server needs fm (and fmd for deployer sites) plus python3. SSH logs in with your "
                "keys or ssh-agent — the same as running ssh in a terminal; passwords aren't supported.",
                "muted",
                wrap=True,
            )
        )
        form = form_layout()
        self.name = QLineEdit(self.host.name, placeholderText="Staging")
        self.destination = QLineEdit(
            self.host.destination, placeholderText="deploy@staging.example.com or an ~/.ssh/config alias"
        )
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("default")
        self.port.setValue(self.host.port)
        self.fm_home = QLineEdit(self.host.fm_home, placeholderText="~/frappe")
        self.production = QCheckBox("Production — show and confirm every change before it runs")
        self.production.setChecked(self.host.production)
        form.addRow("Name", self.name)
        form.addRow("SSH destination", self.destination)
        form.addRow("Port", self.port)
        form.addRow("FM home", self.fm_home)
        form.addRow("", self.production)
        box.addLayout(form)
        self.result_label = label("", "muted", wrap=True)
        box.addWidget(self.result_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.test_btn = button("Test connection", on_click=self._test)
        row = QHBoxLayout()
        row.addWidget(self.test_btn)
        row.addStretch()
        row.addWidget(buttons)
        box.addLayout(row)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

    def _edited(self) -> Host:
        destination = self.destination.text().strip()
        return replace(
            self.host,
            name=self.name.text().strip() or destination,
            destination=destination,
            port=self.port.value(),
            fm_home=self.fm_home.text().strip() or "~/frappe",
            production=self.production.isChecked(),
        )

    def _test(self) -> None:
        host = self._edited()
        if not host.destination:
            self.result_label.setText("Enter an SSH destination first.")
            return
        self.test_btn.setEnabled(False)
        self.result_label.setText(f"Connecting to {host.destination}…")

        def done(raw: dict) -> None:
            self.test_btn.setEnabled(True)
            tools = raw.get("tools", {})
            found = ", ".join(t for t in ("fm", "fmd", "docker") if tools.get(t)) or "none"
            missing = [t for t in ("fm", "docker") if not tools.get(t)]
            text = f"✓ Connected. FM home {raw.get('fm_home')}; tools found: {found}."
            if missing:
                text += f" Missing: {', '.join(missing)} — install them on the server."
            self.result_label.setText(text)

        def failed(message: str) -> None:
            self.test_btn.setEnabled(True)
            self.result_label.setText(f"✗ {message}")

        run_async(lambda: remote.run_probe(host, timeout=30, benches=False, system=False), done, failed)

    def _save(self) -> None:
        host = self._edited()
        if not host.destination:
            self.result_label.setText("Enter an SSH destination.")
            return
        if host.destination.startswith("-") or any(c.isspace() for c in host.destination):
            self.result_label.setText("The SSH destination can't contain spaces or start with '-'.")
            return
        self.host = host
        self.accept()
