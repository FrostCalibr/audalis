"""Main application window: sidebar navigation, pages, status area."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import config, devices, fixes, profiles, rules
from . import components as c
from . import pages as pg
from .sound import SoundTester

NAV = [
    ("Output", "OutputPage"),
    ("Input", "InputPage"),
    ("Headset", "HeadsetPage"),
    ("Applications", "ApplicationsPage"),
    ("Profiles", "ProfilesPage"),
    ("Troubleshooting", "TroubleshootingPage"),
    ("Advanced", "AdvancedPage"),
]

STATUS_TTL_MS = 6000


class _FixThread(QThread):
    """Runs a fix off the UI thread so the window stays responsive."""

    finished_steps = Signal(list)
    error = Signal(str)

    def __init__(self, target: str):
        super().__init__()
        self.target = target

    def run(self):
        try:
            steps = fixes.run_fix(self.target)
            self.finished_steps.emit(steps)
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


class MainWindow(QMainWindow):
    headsetConnected = Signal()
    headsetDisconnected = Signal()
    headsetLog = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audalis Audio Control")
        self.resize(980, 720)
        self.setMinimumSize(760, 560)
        self.cfg = config.load()
        self.sound = SoundTester(self)
        self.state = devices.read_system()
        self._pages: dict[str, pg.Page] = {}
        self._watcher: rules.RuleWatcher | None = None
        self._headset_thread: _FixThread | None = None
        self._last_headset_event: str | None = None

        self.sound.toneDone.connect(lambda: self.status("Test tone finished"))
        self.sound.recordDone.connect(self._mic_test_done)
        self.headsetConnected.connect(self._headset_connected)
        self.headsetDisconnected.connect(self._headset_disconnected)
        self.headsetLog.connect(self.status)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._reset_status)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_sidebar()
        root.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.stack = QStackedWidget()
        self.banner = QLabel("")
        self.banner.setStyleSheet("color: #9a4b00; background: #fff3e0; padding: 6px 12px;")
        self.banner.setVisible(False)
        right.addWidget(self.banner)
        right.addWidget(self.stack, stretch=1)
        self.statusbar = QLabel("Ready")
        self.statusbar.setStyleSheet("border-top: 1px solid rgba(127,127,127,0.25); padding: 4px 10px; color: #444;")
        right.addWidget(self.statusbar)
        root.addLayout(right, stretch=1)

        self._build_pages()
        self.show_banner(self.state.issues)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        self.poll()

        auto_switch = bool((self.cfg.get("preferences") or {}).get("headset_auto_switch", True))
        if auto_switch:
            self.set_auto_switch(True)

    def _build_sidebar(self):
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(180)
        self.sidebar.setStyleSheet("background: rgba(127,127,127,0.06); border-right: 1px solid rgba(127,127,127,0.25);")
        lay = QVBoxLayout(self.sidebar)
        lay.setContentsMargins(10, 14, 10, 10)
        lay.setSpacing(4)

        brand = QWidget()
        brow = QHBoxLayout(brand)
        brow.setContentsMargins(0, 0, 0, 0)
        brow.setSpacing(8)
        mark = QLabel("A")
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignCenter)
        mark.setStyleSheet(
            "background: #2f6fed; color: white; border-radius: 8px; font-size: 18pt; font-weight: bold;"
        )
        brow.addWidget(mark, alignment=Qt.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(0)
        brand_title = c.make_label("Audalis", bold=True, size=15)
        words.addWidget(brand_title)
        subtitle = QLabel("Audio control\nfor Linux")
        subtitle.setStyleSheet("color: #888; font-size: 9pt;")
        words.addWidget(subtitle)
        brow.addLayout(words)
        words.addStretch(1)
        lay.addWidget(brand)
        lay.addSpacing(6)
        lay.addWidget(c.hline())
        self.nav_buttons: dict[str, QPushButton] = {}
        for label, _cls in NAV:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 7px 10px; border: none; border-radius: 6px; background: transparent; }"
                "QPushButton:checked { background: rgba(47,111,237,0.15); color: #2f6fed; font-weight: bold; }"
                "QPushButton:hover { background: rgba(127,127,127,0.12); }"
            )
            btn.clicked.connect(lambda _=False, l=label: self.navigate(l))
            lay.addWidget(btn)
            self.nav_buttons[label] = btn
        lay.addStretch(1)
        ver = QLabel(f"v{__version__}")
        ver.setStyleSheet("color: #aaa; font-size: 8pt;")
        lay.addWidget(ver)

    def _build_pages(self):
        for label, clsname in NAV:
            cls = getattr(pg, clsname)
            page = cls(self)
            self._pages[label] = page
            self.stack.addWidget(page)
        self.navigate("Output")

    def navigate(self, label: str):
        for name, btn in self.nav_buttons.items():
            btn.setChecked(name == label)
        self.stack.setCurrentWidget(self._pages[label])
        self._pages[label].refresh(self.state)

    def refresh(self):
        self.poll(force=True)

    def poll(self, force: bool = False):
        state = devices.read_system()
        if state.issues and not self.banner.isVisible():
            self.show_banner(state.issues)
        if self.state is not None and self.state.issues and not state.issues:
            self.banner.setVisible(False)
        self.state = state
        page = self._pages[self._active_nav()]
        page.refresh(state)

    def _active_nav(self) -> str:
        for name, btn in self.nav_buttons.items():
            if btn.isChecked():
                return name
        return "Output"

    def show_banner(self, issues: list[str]):
        if issues:
            self.banner.setText("  ⚠  " + "   ·   ".join(issues))
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def status(self, msg: str):
        self.statusbar.setText(msg)
        self.statusbar.setStyleSheet("border-top: 1px solid rgba(127,127,127,0.25); padding: 4px 10px; color: #444;")
        self._status_timer.start(STATUS_TTL_MS)

    def _reset_status(self):
        self.statusbar.setText("Ready")
        self.statusbar.setStyleSheet("border-top: 1px solid rgba(127,127,127,0.25); padding: 4px 10px; color: #444;")

    def warn(self, msg: str):
        self.statusbar.setText("Warning: " + msg)
        self.statusbar.setStyleSheet("border-top: 1px solid rgba(127,127,127,0.25); padding: 4px 10px; color: #b3261e;")
        self._status_timer.start(2 * STATUS_TTL_MS)
        config.append_log(msg)

    def report(self, ok: bool, msg: str):
        if ok:
            self.status(msg)
        else:
            self.warn(msg)

    def set_default_output(self, name: str):
        ok = pulse_set_default("sink", name)
        self.report(ok, f"Default output set to {name}" if ok else f"Failed to set default output: {name}")

    def set_default_input(self, name: str):
        ok = pulse_set_default("source", name)
        self.report(ok, f"Default microphone set to {name}" if ok else f"Failed to set default microphone: {name}")

    def apply_profile(self, name: str):
        snap = (self.cfg.get("presets") or {}).get(name)
        if not snap:
            self.warn(f"Profile '{name}' not found")
            return

        def report(ok: bool, desc: str, detail: str):
            self.report(ok, desc + ("" if ok else f". Failed: {detail}"))

        errors = profiles.apply_preset_step(snap, on_result=report)
        if errors:
            self.warn(f"Profile '{name}' applied with {len(errors)} problem(s)")
        else:
            self.status(f"Profile '{name}' applied")
        self.refresh()

    def run_fix(self, target: str):
        self.navigate("Troubleshooting")
        self._pages["Troubleshooting"]._run_fix(target)

    def set_auto_switch(self, enabled: bool):
        if enabled:
            if self._watcher is None or not self._watcher.is_alive():
                self._watcher = rules.RuleWatcher(
                    on_connect=self.headsetConnected.emit,
                    on_disconnect=self.headsetDisconnected.emit,
                    on_log=self.headsetLog.emit,
                )
                self._watcher.start()

    def _headset_connected(self):
        self.status("Headset connected. Routing audio to it.")
        self._last_headset_event = "connected at " + self._now()
        if self._headset_thread is not None and self._headset_thread.isRunning():
            return
        self._headset_thread = _FixThread("headphones")
        self._headset_thread.finished_steps.connect(self._on_headset_steps)
        self._headset_thread.error.connect(lambda e: self.warn("Headset routing failed: " + e))
        self._headset_thread.start()

    def _now(self) -> str:
        from datetime import datetime
        return datetime.now().strftime("%H:%M")

    def _on_headset_steps(self, steps):
        for s in steps:
            self.report(s.ok, s.description)
        ok = all(s.ok for s in steps)
        if ok:
            self.status("Headset connected. Sound and microphone now use it.")
        else:
            self.warn("Headset routing finished with problems.")
        self.refresh()

    def _headset_disconnected(self):
        self.status("Headset disconnected")
        self._last_headset_event = "disconnected at " + self._now()
        self.refresh()

    def _mic_test_done(self, ok: bool):
        if ok:
            self.status("Microphone test finished. You heard your recording.")
        else:
            self.warn("Microphone test failed. Check the microphone and try again.")


def pulse_set_default(kind: str, name: str) -> bool:
    from ..core.audio import pulse

    if kind == "sink":
        return pulse.set_default_sink(name)
    return pulse.set_default_source(name)