"""Lossless, schema-backed native forms for base resumes."""

from __future__ import annotations

import copy
from typing import get_args, get_origin
from uuid import uuid4

from pydantic import BaseModel
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .models import BaseResume


def _label(name: str) -> str:
    return name.replace("_", " ").capitalize()


class ListEditor(QWidget):
    """Ordered strings or records; record IDs survive edits and reordering."""

    dirty = Signal()

    def __init__(self, item_type: type, parent=None):
        super().__init__(parent)
        self.item_type = item_type
        self._items: list = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMinimumHeight(100)
        layout.addWidget(self.list)
        actions = QHBoxLayout()
        for title, callback in (
            ("Add", self.add), ("Edit", self.edit), ("Remove", self.remove),
            ("↑", lambda: self.move(-1)), ("↓", lambda: self.move(1)),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.list.itemDoubleClicked.connect(self.edit)

    def set_data(self, items: list):
        self._items = copy.deepcopy(items)
        self._refresh()

    def data(self) -> list:
        return copy.deepcopy(self._items)

    def _refresh(self, selected=-1):
        self.list.clear()
        for item in self._items:
            if isinstance(item, dict):
                title = next(
                    (str(item[k]) for k in ("text", "title", "qualification", "heading", "id") if item.get(k)),
                    "New entry",
                )
                if item.get("employer"):
                    title += f" — {item['employer']}"
            else:
                title = str(item)
            self.list.addItem(title.replace("\n", " ")[:160])
        self.list.setCurrentRow(selected)

    def _dialog(self, value):
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit entry")
        dialog.resize(640, 560)
        layout = QVBoxLayout(dialog)
        if self.item_type is str:
            editor = QPlainTextEdit()
            editor.setPlainText(value or "")
            layout.addWidget(editor)
            read = editor.toPlainText
        else:
            editor = RecordForm(self.item_type)
            editor.set_data(value or {})
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(editor)
            layout.addWidget(scroll)
            read = editor.data
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        result = []

        def save():
            try:
                item = read()
                if self.item_type is str:
                    if not item.strip():
                        raise ValueError("Entry must not be blank.")
                else:
                    item = self.item_type.model_validate(item).model_dump()
                result.append(item)
                dialog.accept()
            except ValueError as exc:
                QMessageBox.warning(dialog, "Invalid entry", str(exc))

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        return result[0] if result else None

    def add(self):
        item = self._dialog(None)
        if item is not None:
            self._items.append(item)
            self._refresh(len(self._items) - 1)
            self.dirty.emit()

    def edit(self, *_):
        row = self.list.currentRow()
        if row < 0:
            return
        item = self._dialog(self._items[row])
        if item is not None:
            self._items[row] = item
            self._refresh(row)
            self.dirty.emit()

    def remove(self):
        row = self.list.currentRow()
        if row >= 0:
            del self._items[row]
            self._refresh(min(row, len(self._items) - 1))
            self.dirty.emit()

    def move(self, delta: int):
        row = self.list.currentRow()
        target = row + delta
        if row >= 0 and 0 <= target < len(self._items):
            self._items[row], self._items[target] = self._items[target], self._items[row]
            self._refresh(target)
            self.dirty.emit()


class RecordForm(QWidget):
    dirty = Signal()

    def __init__(self, model: type[BaseModel], parent=None):
        super().__init__(parent)
        self.model = model
        self.fields: dict[str, QWidget] = {}
        self._loading = False
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for name, field in model.model_fields.items():
            annotation = field.annotation
            if name == "schema_version":
                continue
            if get_origin(annotation) is list:
                widget = ListEditor(get_args(annotation)[0])
                widget.dirty.connect(self._changed)
            elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
                widget = RecordForm(annotation)
                widget.dirty.connect(self._changed)
            elif annotation is bool:
                widget = QCheckBox()
                widget.toggled.connect(self._changed)
            elif name in ("profile", "text", "supporting_details", "background", "details", "references"):
                widget = QPlainTextEdit()
                widget.setMaximumHeight(130)
                widget.textChanged.connect(self._changed)
            else:
                widget = QLineEdit()
                if name == "id":
                    widget.setToolTip("Stable evidence ID. Keep unchanged after using this resume.")
                widget.textChanged.connect(self._changed)
            widget.setObjectName(name)
            self.fields[name] = widget
            layout.addRow(_label(name), widget)
        self.set_data({})

    def _changed(self, *_):
        if not self._loading:
            self.dirty.emit()

    def set_data(self, data: dict):
        self._loading = True
        try:
            for name, widget in self.fields.items():
                field = self.model.model_fields[name]
                default = field.get_default(call_default_factory=True) if not field.is_required() else ""
                value = copy.deepcopy(data.get(name, default))
                if name == "id" and not value:
                    value = str(uuid4())
                if isinstance(widget, ListEditor):
                    widget.set_data(value or [])
                elif isinstance(widget, RecordForm):
                    widget.set_data(value if isinstance(value, dict) else {})
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QPlainTextEdit):
                    widget.setPlainText(value or "")
                else:
                    widget.setText(str(value or ""))
        finally:
            self._loading = False

    def data(self) -> dict:
        result = {}
        if "schema_version" in self.model.model_fields:
            result["schema_version"] = 1
        for name, widget in self.fields.items():
            if isinstance(widget, (ListEditor, RecordForm)):
                result[name] = widget.data()
            elif isinstance(widget, QCheckBox):
                result[name] = widget.isChecked()
            elif isinstance(widget, QPlainTextEdit):
                result[name] = widget.toPlainText()
            else:
                result[name] = widget.text()
        return result


class BaseResumeEditor(RecordForm):
    """All version-one fields, including nested evidence and fixed sections."""

    def __init__(self, parent=None):
        super().__init__(BaseResume, parent)
        self.fields["verified"].setToolTip("Confirm every fact and evidence item is accurate before live generation.")
        self.fields["is_example"].setToolTip("Example resumes cannot be sent to a live provider.")
        self.fields["verified"].clicked.connect(self._confirm_verified)

    def _confirm_verified(self, checked):
        if checked and QMessageBox.question(
            self, "Verify real career facts",
            "Have you checked every identity detail, role, achievement and fixed section against your real career history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            self.fields["verified"].setChecked(False)

    def validated_data(self) -> dict:
        return BaseResume.model_validate(self.data()).model_dump()
