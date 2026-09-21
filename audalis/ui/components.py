"""Reusable GUI components: calm, native-looking, self-explanatory controls."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core import devices as dm
from ..core.audio.base import Device

ACCENT = "#2f6fed"
OK_COLOR = "#1a7f37"
WARN_COLOR = "#b48a00"
FAIL_COLOR = "#b3261e"
MUTED_COLOR = "#9b9b9b"


def make_label(text: str, *, bold: bool = False, size: int = 11, color: Optional[str] = None) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setPointSize(size)
    f.setBold(bold)
    lbl.setFont(f)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    return lbl


LABEL_COLUMN = 150


def make_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setMinimumHeight(28)
    btn.setStyleSheet("QPushButton { padding: 2px 14px; }")
    return btn


class Explore(QLabel):
    """An explanation under a control. Plain language, short."""

    def __init__(self, text: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setStyleSheet("color: #666; font-size: 10pt;")
        self.setContentsMargins(0, 2, 0, 8)


class SectionCard(QFrame):
    """A titled group that reads like a settings screen section."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "SectionCard { background: rgba(127,127,127,0.05); border: 1px solid rgba(127,127,127,0.25); "
            "border-radius: 8px; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        self.title = make_label(title, bold=True, size=13)
        lay.addWidget(self.title)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        lay.addLayout(self.body)

    def row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        self.body.addLayout(row)
        return row

    def labeled_row(self, label_text: str, size: int = 11) -> QHBoxLayout:
        """Row with a fixed-width label column, controls stretch after it."""
        row = self.row()
        lbl = make_label(label_text, size=size)
        lbl.setMinimumWidth(LABEL_COLUMN)
        lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        row.addWidget(lbl)
        return row

    def add(self, widget: QWidget) -> None:
        self.body.addWidget(widget)


class SettingsGrid(QWidget):
    """A settings-table layout with shared columns.

    Column 0 holds labels, column 1 holds the controls and stretches with
    the window, and column 2 holds right-aligned values at a fixed width.
    All rows share the same columns, so every control starts and ends at the
    same X position and every value lines up. No per-widget padding hacks.
    """

    LABEL = 0
    CONTROL = 1
    VALUE = 2

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(8)
        self._grid.setColumnStretch(self.CONTROL, 1)
        self._grid.setColumnMinimumWidth(self.LABEL, LABEL_COLUMN)
        self._grid.setColumnMinimumWidth(self.VALUE, 96)
        self._rows = 0

    def add_row(self, label: str, control: Optional[QWidget] = None, value: Optional[str] = None, *, value_color: Optional[str] = None) -> None:
        row = self._rows
        self._rows += 1
        lbl = make_label(label)
        self._grid.addWidget(lbl, row, self.LABEL, Qt.AlignLeft | Qt.AlignVCenter)
        if control is not None:
            self._grid.addWidget(control, row, self.CONTROL, Qt.AlignVCenter)
        if value is not None:
            val = make_label(value, size=10, color=value_color)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._grid.addWidget(val, row, self.VALUE, Qt.AlignRight | Qt.AlignVCenter)

    def add_slider_row(self, label: str, percent: int, on_commit) -> QLabel:
        """A stretching slider row; its live percentage lives in the value column."""
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(max(0, min(100, percent)))
        slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        readout = make_label(f"{slider.value()}%", size=10)
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda v: readout.setText(f"{v}%"))
        slider.sliderReleased.connect(lambda: on_commit(slider.value()))
        self.add_row(label, slider)
        self._grid.addWidget(readout, self._rows - 1, self.VALUE, Qt.AlignRight | Qt.AlignVCenter)
        return readout


class VolumeSlider(QWidget):
    """Volume slider with a live percentage and a mute toggle.

    Emits committed(pct) when the user releases the handle, so dragging
    does not spam pactl.
    """

    committed = Signal(int)
    toggled = Signal(bool)

    def __init__(self, label: str = "", *, show_mute: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        if label:
            self.label = make_label(label, bold=True)
            self.label.setMinimumWidth(LABEL_COLUMN)
            self.label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            lay.addWidget(self.label)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setMinimumWidth(180)
        self.slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._muted = False
        self.pct = make_label("0%", size=10)
        self.pct.setMinimumWidth(36)
        lay.addWidget(self.slider, stretch=1)
        lay.addWidget(self.pct)

        self.mute: Optional[QPushButton] = None
        if show_mute:
            self.mute = make_button("Mute")
            self.mute.setCheckable(True)
            self.mute.setFixedWidth(72)
            lay.addWidget(self.mute)
        lay.addStretch(0)

        self.slider.valueChanged.connect(self._on_drag)
        self.slider.sliderReleased.connect(self._on_release)
        if show_mute:
            self.mute.toggled.connect(lambda v: self.toggled.emit(v))

    def _on_drag(self, value: int) -> None:
        self.pct.setText(f"{value}%")
        if self._muted:
            self.pct.setStyleSheet("color: " + MUTED_COLOR)

    def _on_release(self) -> None:
        self.committed.emit(self.slider.value())

    def set_state(self, value: int, muted: bool) -> None:
        self._muted = muted
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.pct.setText(f"{value}%")
        self.pct.setStyleSheet("color: " + (MUTED_COLOR if muted else ""))
        if self.mute is not None:
            self.mute.blockSignals(True)
            self.mute.setChecked(muted)
            self.mute.blockSignals(False)
            self.mute.setText("Unmute" if muted else "Mute")


class BalanceSlider(QWidget):
    """Left/right balance. Emits committed(value) for -100..100 on release."""

    committed = Signal(int)

    def __init__(self, label: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        if label:
            self.label = make_label(label, bold=True)
            self.label.setMinimumWidth(LABEL_COLUMN)
            self.label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            lay.addWidget(self.label)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-50, 50)
        self.slider.setMinimumWidth(180)
        self.slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.readout = make_label("Center", size=10)
        self.readout.setMinimumWidth(52)
        lay.addWidget(self.slider, stretch=1)
        lay.addWidget(self.readout)
        lay.addStretch(0)
        self.slider.valueChanged.connect(self._live)
        self.slider.sliderReleased.connect(lambda: self.committed.emit(self.slider.value()))

    def _live(self, v: int) -> None:
        if v == 0:
            self.readout.setText("Center")
        elif v < 0:
            self.readout.setText(f"Left {abs(v)}")
        else:
            self.readout.setText(f"Right {v}")

    def set_balance(self, value: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(max(-50, min(50, value)))
        self.slider.blockSignals(False)
        self._live(self.slider.value())


class DeviceChooser(QWidget):
    """A dropdown listing devices in plain language."""

    selected = Signal(str)  # raw device key

    def __init__(self, label: str = "", explanation: Optional[QWidget] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        if label:
            row.addWidget(make_label(label, bold=True))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(300)
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.combo, stretch=1)
        lay.addLayout(row)
        if explanation is not None:
            lay.addWidget(explanation)
        self._names: list[str] = []
        self.combo.currentIndexChanged.connect(self._emit)

    def _emit(self, idx: int) -> None:
        if 0 <= idx < len(self._names):
            self.selected.emit(self._names[idx])

    def set_options(self, options: list[tuple[str, str, str]], current: str) -> None:
        """options = [(key, label, tooltip), ...]; current = key to preselect."""
        self.combo.blockSignals(True)
        self._names = [key for key, _label, _tip in options]
        self.combo.clear()
        for key, label, tooltip in options:
            self.combo.addItem(label)
            idx = self.combo.count() - 1
            if tooltip:
                self.combo.setItemData(idx, tooltip, Qt.ToolTipRole)
            self.combo.setItemData(idx, key, Qt.UserRole)
        idx = self._names.index(current) if current in self._names else 0
        self.combo.setCurrentIndex(max(0, idx))
        self.combo.blockSignals(False)


class StatusDot(QLabel):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("●", parent)
        self.setStyleSheet(f"color: {OK_COLOR}; font-size: 12pt;")


class FixRow(QWidget):
    """One line of a diagnosis or fix log: status dot + text."""

    def __init__(self, ok: bool, text: str, detail: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        dot = QLabel("✓" if ok else "✗")
        dot.setStyleSheet(f"color: {OK_COLOR}; font-weight: bold;" if ok else f"color: {FAIL_COLOR}; font-weight: bold;")
        lay.addWidget(dot, alignment=Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(make_label(text))
        if detail:
            col.addWidget(Explore(detail))
        lay.addLayout(col, stretch=1)


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color: rgba(127,127,127,0.35);")
    return f