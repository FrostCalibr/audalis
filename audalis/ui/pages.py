"""UI pages: Output, Input, Headset, Applications, Profiles, Troubleshooting, Advanced.

Each page is rebuilt only when its underlying device signature changes;
otherwise widget values are updated in place so slider drags aren't disturbed.
"""

from __future__ import annotations

import re
import threading
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import config, devices, diagnostics as diag_mod, fixes, profiles, rules
from ..core.audio import alsa, pulse
from ..core.audio.base import Device
from . import components as c
from .sound import SoundTester


def _human_profile(profile: str) -> str:
    mapping = {
        "output:analog-stereo": "Analog output",
        "output:hdmi-stereo": "HDMI output",
        "output:analog-stereo+input:analog-stereo": "Speakers + microphone",
        "output:hdmi-stereo+input:analog-stereo": "HDMI output + microphone",
    }
    return mapping.get(profile, profile)


def device_fingerprint(state) -> tuple:
    return (
        tuple((d.name, d.active_port, d.muted, d.volume_pct, d.state) for d in state.outputs + state.inputs),
        tuple((c.name, c.active_profile) for c in state.cards),
    )


def balance_of(dev) -> int:
    if not dev or len(dev.channels) < 2:
        return 0
    a, b = dev.channels[0], dev.channels[1]
    if a <= 0 or b <= 0 or a == b:
        return 0
    if a < b:
        return round((1 - a / b) * 100)
    return round(-(1 - b / a) * 100)


class Page(QWidget):
    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self._sig: object = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.content = QVBoxLayout(self.container)
        self.content.setSpacing(10)
        self.content.setContentsMargins(16, 12, 16, 16)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll)

    def refresh(self, state) -> None:
        sig = self.signature(state)
        try:
            if sig != self._sig:
                self._sig = sig
                self.build(state)
            else:
                self.update_(state)
        except Exception as exc:  # noqa: BLE001
            self.app.warn(f"{self.__class__.__name__} refresh failed: {exc}")

    def signature(self, state) -> object:
        return None

    def build(self, state) -> None:
        raise NotImplementedError

    def update_(self, state) -> None:
        pass

    def _clear(self) -> None:
        while self.content.count():
            item = self.content.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _add_stretch(self) -> None:
        self.content.addStretch(1)


# --------------------------------------------------------------------------- Output

class OutputPage(Page):
    def signature(self, state):
        return (device_fingerprint(state), state.default_sink)

    def build(self, state):
        self._clear()
        self._out_devices = {d.name: d for d in state.outputs}
        opts = devices.output_options(state)
        self._options = {o.key: o for o in opts}
        if not opts:
            self.content.addWidget(c.make_label("No output devices found."))
            self._add_stretch()
            return
        card = c.SectionCard("Output")
        self.content.addWidget(card)
        self.chooser = c.DeviceChooser("", c.Explore("Where sound goes. Pick speakers, headphones or HDMI."))
        self.chooser.selected.connect(self._choose_output)
        row = card.row()
        row.addWidget(self.chooser, stretch=1)
        active = next((o for o in opts if o.active), opts[0])
        self.chooser.set_options([(o.key, o.label, o.detail) for o in opts], active.key)
        self._selected_key = active.key

        card2 = c.SectionCard("Volume")
        self.content.addWidget(card2)
        vol_row = card2.row()
        self.vol = c.VolumeSlider()
        self.vol.committed.connect(self._set_volume)
        self.vol.toggled.connect(self._set_mute)
        vol_row.addWidget(self.vol, stretch=1)
        card2.add(c.Explore("How loud output plays. Applications keep their own volumes; this is the overall output level."))

        bal_section = c.SectionCard("Balance")
        self.content.addWidget(bal_section)
        b_row = bal_section.row()
        self.balance = c.BalanceSlider()
        self.balance.committed.connect(self._set_balance)
        b_row.addWidget(self.balance, stretch=1)
        bal_section.add(c.Explore("Moves sound between the left and right channels if your setup is unbalanced."))

        test = c.SectionCard("Test sound")
        self.content.addWidget(test)
        t_row = test.row()
        btn = c.make_button("Test sound")
        btn.clicked.connect(lambda: (self.app.status("Playing a test tone."), self.app.sound.play_tone()))
        t_row.addWidget(btn, alignment=Qt.AlignLeft)
        test.add(c.Explore("Plays a short tone so you can hear whether the selected output works."))
        self._apply_state(state)
        self._add_stretch()

    def update_(self, state):
        self._out_devices = {d.name: d for d in state.outputs}
        default = state.default_output()
        if default:
            self._apply_state(state)

    def _current_option(self) -> Optional[devices.OutputOption]:
        idx = self.chooser.combo.currentIndex()
        names = getattr(self.chooser, "_names", [])
        key = names[idx] if 0 <= idx < len(names) else None
        for o in self._options.values():
            if o.key == key:
                return o
        return None

    def _live_output(self) -> Optional[Device]:
        opt = self._current_option()
        if opt and opt.is_live:
            return self._out_devices.get(opt.key) or opt.live
        default = self.app.state.default_output()
        return default

    def _apply_state(self, state):
        dev = state.default_output()
        if not dev:
            return
        selected = self._current_option() if hasattr(self, "chooser") else None
        if selected is not None and not selected.is_live:
            self.vol.setEnabled(False)
            self.balance.setEnabled(False)
            return
        self.vol.setEnabled(True)
        self.balance.setEnabled(True)
        self.vol.set_state(dev.volume_pct, dev.muted)
        self.balance.set_balance(balance_of(dev))

    def _choose_output(self, key: str):
        self._selected_key = key
        opt = self._options.get(key)
        if not opt:
            return
        ok = devices.select_output(self.app.state, opt)
        if ok:
            self.app.status(("Output set to " if opt.is_live else "Switched to ") + opt.label + ("." if opt.is_live else " (audio mode changed)."))
            self.app.sound.play_confirm()
        else:
            self.app.warn(f"Failed to switch output to {opt.label}.")
        self.app.refresh()

    def _set_volume(self, pct):
        dev = self._live_output()
        if dev is None:
            return
        ok = pulse.set_volume("sink", dev.name, pct)
        self.app.report(ok, f"Set output volume to {pct}%" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _set_mute(self, muted):
        dev = self._live_output()
        if dev is None:
            return
        ok = pulse.set_mute_state("sink", dev.name, muted)
        self.app.report(ok, "Output " + ("muted" if muted else "unmuted") + ("" if ok else ". Failed."))

    def _set_balance(self, bal):
        dev = self._live_output()
        if dev is None:
            return
        ok = pulse.set_balance("sink", dev.name, bal, dev.volume_pct, len(dev.channels) or 2)
        self.app.report(ok, "Balance set" + ("" if ok else ". Failed."))
        self.app.refresh()


# --------------------------------------------------------------------------- Input

class InputPage(Page):
    def signature(self, state):
        return (device_fingerprint(state), state.default_source, self._boost(state))

    def _boost(self, state):
        out = []
        for d in state.inputs:
            card = state.card_for_device(d)
            if card and card.alsa_card:
                info = devices.mic_boost_info(card)
                if info:
                    out.append((card.alsa_card, info["current_db"], list(info["values"])))
        return out

    def build(self, state):
        self._clear()
        self._in_devices = {d.name: d for d in state.inputs}
        opts = devices.input_options(state)
        self._in_options = {o.key: o for o in opts}
        if not opts:
            self.content.addWidget(c.make_label("No microphone (input) devices found."))
            self._add_stretch()
            return
        card = c.SectionCard("Microphone")
        self.content.addWidget(card)
        self.chooser = c.DeviceChooser("", c.Explore("The device that captures your voice for calls, recordings and voice assistants."))
        self.chooser.selected.connect(self._choose_input)
        row = card.row()
        row.addWidget(self.chooser, stretch=1)
        active = next((o for o in opts if o.active), opts[0])
        self.chooser.set_options([(o.key, o.label, o.detail) for o in opts], active.key)

        vol_card = c.SectionCard("Input volume")
        self.content.addWidget(vol_card)
        vr = vol_card.row()
        self.vol = c.VolumeSlider()
        self.vol.committed.connect(self._set_volume)
        self.vol.toggled.connect(self._set_mute)
        vr.addWidget(self.vol, stretch=1)
        vol_card.add(c.Explore("How loud you sound to apps. Raise it if people say you are quiet. Lower it if you are too loud."))

        self._boost_row = None
        self._build_boost(state)
        self._apply_state(state)
        self._add_stretch()

    def _choose_input(self, key: str):
        opt = self._in_options.get(key)
        if not opt:
            return
        ok = devices.select_input(self.app.state, opt)
        self.app.report(ok, f"Microphone set to {opt.label}" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _build_boost(self, state, parent=None):
        default = state.default_input()
        card = state.card_for_device(default) if default else None
        if not card or not card.alsa_card:
            return
        info = devices.mic_boost_info(card)
        if not info:
            return
        boost = c.SectionCard("Microphone boost")
        self.content.addWidget(boost)
        b_row = boost.labeled_row("Microphone boost")
        combo = QComboBox()
        combo.addItems(info["values"])
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        idx = 0
        best = -1
        for i, v in enumerate(info["values"]):
            if v.startswith(f"{info['current_db']:g}") or abs(float(v.split()[0]) - info["current_db"]) < 0.01:
                best = i
        combo.setCurrentIndex(best if best >= 0 else 0)
        element = info["element"]
        combo.currentIndexChanged.connect(lambda i, e=element, c=card: self._set_boost(c, e, i))
        b_row.addWidget(combo, stretch=1)
        boost.add(c.Explore("Makes your microphone louder before audio reaches applications. Higher values help when you are quiet, but they can add background noise."))

    def _set_boost(self, card, element, index):
        ok = alsa.cset(card.alsa_card, element.numid, alsa.boost_value_for(element, index))
        self.app.report(ok, "Microphone boost set" + ("" if ok else ". Failed."))
        self.app.refresh()

    def update_(self, state):
        self._in_devices = {d.name: d for d in state.inputs}
        default = state.default_input()
        if default:
            self.vol.set_state(default.volume_pct, default.muted)

    def _apply_state(self, state):
        default = state.default_input()
        if default:
            self.vol.set_state(default.volume_pct, default.muted)

    def _current_device(self):
        chooser = getattr(self, "chooser", None)
        if chooser is None:
            return self.app.state.default_input()
        idx = chooser.combo.currentIndex()
        names = getattr(chooser, "_names", [])
        key = names[idx] if 0 <= idx < len(names) else None
        opt = self._in_options.get(key) if key else None
        if opt is None:
            return self.app.state.default_input()
        return self._in_devices.get(opt.key) or opt.live

    def _set_volume(self, pct):
        dev = self._current_device()
        if dev is None:
            return
        ok = pulse.set_volume("source", dev.name, pct)
        self.app.report(ok, f"Set microphone volume to {pct}%" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _set_mute(self, muted):
        dev = self._current_device()
        if dev is None:
            return
        ok = pulse.set_mute_state("source", dev.name, muted)
        self.app.report(ok, "Microphone " + ("muted" if muted else "unmuted") + ("" if ok else ". Failed."))


# --------------------------------------------------------------------------- Headset

class HeadsetPage(Page):
    def signature(self, state):
        return [self._headset(state)]

    def _headset(self, state):
        out = []
        for card in state.cards:
            for pin in devices.jack_pins(card):
                out.append((pin["name"], pin["present"]))
        return out

    def build(self, state):
        self._clear()
        pins = {
            (p["name"] or "").lower(): p["present"]
            for card in state.cards
            for p in devices.jack_pins(card)
        }
        hp = pins.get("headphone jack", pins.get("front headphone jack"))
        mic = pins.get("mic jack", pins.get("front mic jack"))

        status = c.SectionCard("Connection")
        self.content.addWidget(status)
        row = status.row()
        if hp is True:
            row.addWidget(c.make_label("● Headphones connected", color=c.OK_COLOR))
        elif hp is False:
            row.addWidget(c.make_label("○ No headphones connected", color=c.MUTED_COLOR))
        else:
            row.addWidget(c.make_label("Headphone connection not detected on this hardware"))
        row = status.row()
        if mic is True:
            row.addWidget(c.make_label("● Headset microphone available", color=c.OK_COLOR))
        elif mic is False:
            row.addWidget(c.make_label("No headset microphone connected", color=c.MUTED_COLOR))
        last = getattr(self.app, "_last_headset_event", None)
        if last:
            row = status.row()
            row.addWidget(c.make_label("Last change: " + last, color="#666", size=10))

        sw = c.SectionCard("Automatic switching")
        self.content.addWidget(sw)
        s_row = sw.row()
        s_row.addWidget(c.make_label("Switch to headset when plugged in"))
        self.auto_switch = c.make_button("OFF")
        self.auto_switch.setCheckable(True)
        self.auto_switch.setFixedWidth(88)
        enabled = bool((self.app.cfg.get("preferences") or {}).get("headset_auto_switch", True))
        self.auto_switch.setChecked(enabled)
        self.auto_switch.setText("ON" if enabled else "OFF")
        self.auto_switch.clicked.connect(self._toggle_auto)
        s_row.addWidget(self.auto_switch)
        s_row.addStretch(1)
        sw.add(c.Explore("When you plug in headphones with a microphone, audalis routes sound and the microphone to them automatically."))

        fix = c.SectionCard("Fix your headset")
        self.content.addWidget(fix)
        f_row = fix.row()
        btn = c.make_button("Fix headphones")
        btn.clicked.connect(lambda: self.app.run_fix("headphones"))
        f_row.addWidget(btn, alignment=Qt.AlignLeft)
        fix.add(c.Explore("Sets the correct audio mode and routes output to the headphone jack. Use this if plugging in headphones doesn't change where sound comes out."))
        self._add_stretch()

    def _toggle_auto(self, checked):
        self.auto_switch.setText("ON" if checked else "OFF")
        self.app.cfg.setdefault("preferences", {})["headset_auto_switch"] = checked
        config.save(self.app.cfg)
        self.app.set_auto_switch(checked)
        self.app.status("Automatic switching " + ("enabled" if checked else "disabled"))


# --------------------------------------------------------------------------- Applications

class ApplicationsPage(Page):
    def signature(self, state):
        streams = pulse.get_app_streams()
        return [(s.index, s.direction, s.volume_pct, s.muted, s.target) for s in streams]

    def build(self, state):
        self._clear()
        streams = pulse.get_app_streams()
        if not streams:
            self.content.addWidget(c.make_label("No applications are currently playing or recording audio."))
            self._add_stretch()
            return
        playing = [s for s in streams if s.direction == "sink-input"]
        recording = [s for s in streams if s.direction == "source-output"]
        if playing:
            sc = c.SectionCard("Playing")
            self.content.addWidget(sc)
            for s in playing:
                sl = c.VolumeSlider(s.label)
                sl.set_state(s.volume_pct, s.muted)
                sl.committed.connect(lambda p, st=s: self._set_stream(st, p))
                sl.toggled.connect(lambda m, st=s: self._set_stream_mute(st, m))
                sc.add(sl)
        if recording:
            sc = c.SectionCard("Recording")
            self.content.addWidget(sc)
            for s in recording:
                sl = c.VolumeSlider(s.label)
                sl.set_state(s.volume_pct, s.muted)
                sl.committed.connect(lambda p, st=s: self._set_stream(st, p))
                sl.toggled.connect(lambda m, st=s: self._set_stream_mute(st, m))
                sc.add(sl)
        self._add_stretch()

    def _set_stream(self, s, pct):
        ok = pulse.set_stream_volume(s.direction, s.index, pct)
        self.app.report(ok, f"Set {s.label} volume to {pct}%" + ("" if ok else ". Failed."))

    def _set_stream_mute(self, s, muted):
        ok = pulse.set_stream_mute(s.direction, s.index, muted)
        self.app.report(ok, f"{'Muted' if muted else 'Unmuted'} {s.label}" + ("" if ok else ". Failed."))


# --------------------------------------------------------------------------- Profiles

class ProfilesPage(Page):
    def signature(self, state):
        return tuple(sorted((self.app.cfg.get("presets") or {}).keys()))

    def build(self, state):
        self._clear()
        presets = self.app.cfg.get("presets") or {}
        if not presets:
            self.content.addWidget(c.make_label("No saved profiles yet. Configure your audio, then save it as a profile to restore with one click."))
        startup = (self.app.cfg.get("autostart") or {}).get("preset", "")
        for name, snap in presets.items():
            card = c.SectionCard(name)
            self.content.addWidget(card)
            summary = profiles.preset_summary(snap)
            row = card.row()
            matches = profiles.compare_state(profiles.snapshot_current(), snap)
            row.addWidget(c.make_label(("● matches current" if matches else "   "), color=c.OK_COLOR if matches else c.MUTED_COLOR))
            row.addWidget(c.make_label(summary, color="#666", size=10))
            row.addStretch(1)
            apply_btn = c.make_button("Apply")
            apply_btn.clicked.connect(lambda n=name: self.app.apply_profile(n))
            row.addWidget(apply_btn)
            login = c.make_button("Login")
            login.setCheckable(True)
            login.setChecked(name == startup)
            login.setText("Restore at login" if name == startup else "Login")
            login.clicked.connect(lambda checked, n=name: self._toggle_login(n, checked))
            row.addWidget(login)
            del_btn = c.make_button("Delete")
            del_btn.clicked.connect(lambda n=name: self._delete(n))
            row.addWidget(del_btn)
        save_card = c.SectionCard("Save")
        self.content.addWidget(save_card)
        sr = save_card.row()
        self.save_btn = c.make_button("Save current configuration as a profile")
        self.save_btn.clicked.connect(self._save)
        sr.addWidget(self.save_btn, alignment=Qt.AlignLeft)
        save_card.add(c.Explore("Profiles remember which output and microphone are active, their levels and your microphone hardware settings."))
        self._add_stretch()

    def _save(self):
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "Save profile", "Name for this configuration:")
        name = (name or "").strip()
        if not ok or not name:
            return
        snap = profiles.snapshot_current()
        self.app.cfg.setdefault("presets", {})[name] = snap
        config.save(self.app.cfg)
        self.app.status(f"Profile '{name}' saved")
        self.app.refresh()

    def _toggle_login(self, name, on):
        self.app.cfg.setdefault("autostart", {})["preset"] = name if on else ""
        config.save(self.app.cfg)
        config.write_autostart(name if on else "")
        self.app.status(("Restores "+name+" at login" if on else "Login restore disabled"))
        self.app.refresh()

    def _delete(self, name):
        presets = self.app.cfg.get("presets") or {}
        presets.pop(name, None)
        if (self.app.cfg.get("autostart") or {}).get("preset") == name:
            self.app.cfg["autostart"]["preset"] = ""
            config.write_autostart("")
        config.save(self.app.cfg)
        self.app.status(f"Profile '{name}' deleted")
        self.app.refresh()


# --------------------------------------------------------------------------- Troubleshooting

class _FixThread(QThread):
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


class TroubleshootingPage(Page):
    # Diagnosis reruns whenever device state changes, so the fix log is only
    # wiped on real rebuilds (fresh diagnosis), never every poll tick.
    def signature(self, state):
        return device_fingerprint(state)

    def build(self, state):
        self._clear()
        head = c.SectionCard("Diagnosis")
        self.content.addWidget(head)
        diag = diag_mod.run_diagnosis()
        for ch in diag.checks:
            ok = ch.status in ("ok", "info")
            warn = ch.status == "warn"
            head.add(c.FixRow(ok, ch.title, ch.detail))
        if not diag.checks:
            head.add(c.make_label("Could not read audio state."))
        else:
            if diag.all_ok:
                head.add(c.make_label("Everything looks fine.", color=c.OK_COLOR))
            else:
                fixes_needed = diag.supported_fixes()
                for f in fixes_needed:
                    name = {"microphone": "Fix microphone", "headphones": "Fix headphones"}.get(f, f)
                    btn = c.make_button(name)
                    btn.clicked.connect(lambda _, t=f: self._run_fix(t))
                    head.row().addWidget(btn, alignment=Qt.AlignLeft)

        mic = c.SectionCard("Test")
        self.content.addWidget(mic)
        mrow = mic.row()
        mt = c.make_button("Test microphone")
        mt.clicked.connect(self._test_mic)
        mrow.addWidget(mt, alignment=Qt.AlignLeft)
        mic.add(c.Explore("Records a few seconds from your microphone, then plays them back so you can hear what others hear."))

        self._log = QPlainTextEdit()
        self._log.setFixedHeight(160)
        self._log.setReadOnly(True)
        if hasattr(self, "_last_log"):
            self._log.setPlainText(self._last_log)
        log_card = c.SectionCard("Fix log")
        self.content.addWidget(log_card)
        log_card.add(self._log)
        self._add_stretch()

    def _run_fix(self, target):
        self._log.setPlainText("")
        self._thread = _FixThread(target)
        self._thread.finished_steps.connect(self._on_steps)
        self._thread.error.connect(lambda e: self._log.appendPlainText("error: " + e))
        self._thread.start()
        self.app.status(f"Fixing {target}.")

    def _on_steps(self, steps):
        lines = []
        for s in steps:
            mark = "✓" if s.ok else "✗"
            lines.append(f"{mark} {s.description}" + (f"  - {s.detail}" if s.detail else ""))
        self._last_log = "\n".join(lines)
        self._log.setPlainText(self._last_log)
        ok = all(s.ok for s in steps)
        self.app.report(ok, "Fix " + ("complete" if ok else "had problems"))
        self.app.refresh()
        if ok:
            self.app.status("Done. Test your microphone to confirm.")

    def _test_mic(self):
        if not self.app.sound.mic_available():
            self.app.warn("parec is not installed. Install PulseAudio utilities to test the microphone.")
            return
        self.app.status("Recording a few seconds. Speak now.")
        def _work():
            self.app.sound.record_and_play(3.0)
        threading.Thread(target=_work, daemon=True).start()


# --------------------------------------------------------------------------- Advanced

class AdvancedPage(Page):
    def signature(self, state):
        return (
            [(c.name, c.active_profile) for c in state.cards],
            [(d.name, d.active_port) for d in state.outputs + state.inputs],
        )

    def build(self, state):
        self._clear()
        note = c.make_label("Expert access to the underlying audio configuration. Changes here affect technical settings directly.", color="#666")
        self.content.addWidget(note)

        for card in state.cards:
            sc = c.SectionCard("Audio mode: " + card.description)
            self.content.addWidget(sc)
            if card.profiles:
                row = sc.labeled_row("Audio mode")
                combo = QComboBox()
                combo.addItems([p.name for p in card.profiles])
                names = [p.name for p in card.profiles]
                cur = card.active_profile
                combo.setCurrentIndex(names.index(cur) if cur in names else 0)
                combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                combo.currentIndexChanged.connect(lambda i, c=card, n=names: self._set_profile(c, n[i]))
                row.addWidget(combo, stretch=1)
            row = sc.labeled_row("Card")
            row.addWidget(c.make_label(card.name, color="#888", size=9))
            row.addStretch(1)

            for dev in state.outputs + state.inputs:
                if dev.alsa_card != card.alsa_card:
                    continue
                kind = "sink" if dev.direction == "sink" else "source"
                for port in dev.ports:
                    avail = "✓" if port.available else ("✗ not detected" if port.availability != "unknown" else "?")
                    color = c.OK_COLOR if port.available else (c.FAIL_COLOR if port.availability != "unknown" else c.MUTED_COLOR)
                    row = sc.labeled_row(f"{dir_label(dev)}: {port.description}")
                    row.addWidget(c.make_label(port.name + "   " + avail, size=9, color=color), stretch=1)
                    b = c.make_button("Force")
                    b.setFixedWidth(64)
                    b.clicked.connect(lambda _, k=kind, d=dev.name, p=port.name: self._force_port(k, d, p))
                    row.addWidget(b)

        for card in state.cards:
            if not card.alsa_card:
                continue
            els, _ = alsa.get_mixer_elements(card.alsa_card)
            controls = [e for e in els if e.mutable and not alsa.is_advanced_only(e.name) and e.ekind in ("BOOLEAN", "INTEGER", "ENUMERATED")]
            pins = [e for e in els if alsa.is_jack_pin(e)]
            if not controls and not pins:
                continue
            sc = c.SectionCard("Hardware controls: " + card.alsa_card_name)
            self.content.addWidget(sc)
            grid = c.SettingsGrid()
            sc.add(grid)
            for e in controls:
                if e.is_boolean:
                    b = c.make_button("ON" if str(e.current).lower() == "on" else "OFF")
                    b.setFixedWidth(64)
                    b.clicked.connect(lambda _, el=e: self._toggle_el(card, el))
                    grid.add_row(e.name, b)
                elif e.is_enum:
                    combo = QComboBox()
                    combo.addItems(e.items or [])
                    try:
                        ci = int(e.current) if str(e.current).isdigit() else (e.items.index(str(e.current)) if str(e.current) in e.items else 0)
                    except ValueError:
                        ci = 0
                    combo.setCurrentIndex(ci)
                    combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                    combo.currentIndexChanged.connect(lambda i, el=e, cn=card: self._set_enum(cn, el, i))
                    grid.add_row(e.name, combo)
                elif e.is_integer:
                    m = re.search(r"(?:=\s*)(\d+)%", alsa.display_text(e))
                    pct = m.group(1) if m else str(round((int(e.current) - e.min) * 100 / max(1, e.max - e.min)))
                    grid.add_slider_row(e.name, int(pct) if pct.isdigit() else 0, lambda v, el=e, cn=card: self._set_int(cn, el, v))
            for pin in pins:
                present = pin.current == "on"
                grid.add_row(pin.name, value="plugged / active" if present else "nothing detected", value_color=c.OK_COLOR if present else c.MUTED_COLOR)

        dbg = c.SectionCard("Debug information")
        self.content.addWidget(dbg)
        drow = dbg.row()
        b_ref = c.make_button("Refresh")
        b_ref.clicked.connect(self._refresh_debug)
        drow.addWidget(b_ref, alignment=Qt.AlignLeft)
        b_copy = c.make_button("Copy to clipboard")
        b_copy.clicked.connect(self._copy_debug)
        drow.addWidget(b_copy, alignment=Qt.AlignLeft)
        drow.addStretch(1)
        self._debug_text = QPlainTextEdit()
        self._debug_text.setReadOnly(True)
        self._debug_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._debug_text.setStyleSheet("font-family: monospace; font-size: 9pt;")
        self._debug_text.setFixedHeight(240)
        dbg.add(self._debug_text)
        self._refresh_debug()
        self._add_stretch()

    def _refresh_debug(self):
        self._debug_text.setPlainText(diag_mod.debug_report())
        self.app.status("Debug report refreshed.")

    def _copy_debug(self):
        QApplication.clipboard().setText(self._debug_text.toPlainText())
        self.app.status("Debug report copied to the clipboard.")

    def _set_profile(self, card, name):
        ok = pulse.set_card_profile(card.name, name)
        self.app.report(ok, f"Set {card.description} audio mode" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _force_port(self, kind, dev, port):
        ok = pulse.set_port(kind, dev, port)
        self.app.report(ok, f"Forced {port} on {dev}" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _toggle_el(self, card, el):
        value = "off" if str(el.current).lower() == "on" else "on"
        ok = alsa.cset(card.alsa_card, el.numid, value)
        self.app.report(ok, f"{el.name} set to {value}" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _set_enum(self, card, el, idx):
        ok = alsa.cset(card.alsa_card, el.numid, str(idx))
        self.app.report(ok, f"{el.name} set to {el.items[idx]}" + ("" if ok else ". Failed."))
        self.app.refresh()

    def _set_int(self, card, el, pct):
        mn, mx = el.min, el.max
        raw = mn + round((mx - mn) * pct / 100)
        ok = alsa.cset(card.alsa_card, el.numid, raw)
        self.app.report(ok, f"{el.name} set to {pct}%" + ("" if ok else ". Failed."))
        self.app.refresh()


def dir_label(dev) -> str:
    return "Output" if dev.direction == "sink" else "Input"