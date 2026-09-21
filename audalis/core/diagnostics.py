"""Diagnostics: report what audio hardware exists and whether it works.

Every check reports a human title, a status (ok / warn / fail / info) and a
plain-language explanation. Fixes are referenced by id so the UI can offer a
guided repair for exactly the problems found.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from . import config, devices
from .audio import alsa, pulse
from .audio.base import Device

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"


@dataclasses.dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str = ""
    fix: Optional[str] = None


@dataclasses.dataclass
class Diagnosis:
    checks: list[Check] = dataclasses.field(default_factory=list)

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.status in (FAIL, WARN)]

    @property
    def all_ok(self) -> bool:
        return not self.problems

    def supported_fixes(self) -> list[str]:
        fixes: list[str] = []
        for c in self.problems:
            if c.fix and c.fix not in fixes:
                fixes.append(c.fix)
        return fixes


def _analog_sources(state: devices.SystemState) -> list[Device]:
    return [d for d in state.inputs if d.active_port.startswith("analog") or "analog" in d.name]


def _first_alsa_card(state: devices.SystemState):
    for c in state.cards:
        if c.alsa_card:
            return c
    return None


def run_diagnosis() -> Diagnosis:
    state = devices.read_system()
    checks: list[Check] = []

    if state.issues:
        for issue in state.issues:
            checks.append(Check(id="preflight", title=issue, status=FAIL))
        return Diagnosis(checks=checks)

    # --- output ---
    if not state.outputs:
        checks.append(Check("output-present", "Audio output hardware", FAIL, "No output device found.", "headphones"))
    else:
        default_out = state.default_output()
        checks.append(Check("output-present", "Audio output hardware", OK, "Output devices found."))
        if default_out is None:
            checks.append(Check("output-default", "Default output", WARN, "No default output is set."))
        else:
            checks.append(
                Check(
                    "output-default",
                    "Default output",
                    OK if default_out.state != "suspended" else WARN,
                    f"{devices.device_label(default_out)} is the default output.",
                )
            )

    # --- input ---
    if not state.inputs:
        checks.append(Check("input-present", "Microphone hardware", FAIL, "No microphone (input) device found.", "microphone"))
    else:
        checks.append(Check("input-present", "Microphone hardware", OK, "Microphone hardware found."))
        default_in = state.default_input()
        if default_in is None:
            checks.append(Check("input-default", "Default microphone", WARN, "No default microphone is set.", "microphone"))
        else:
            checks.append(
                Check(
                    "input-default",
                    "Default microphone",
                    OK,
                    f"{devices.device_label(default_in)} is the default microphone.",
                )
            )
        an = default_in or (_analog_sources(state)[0] if _analog_sources(state) else None)
        if an is not None:
            checks.extend(_input_checks(an, state))

    # --- headset / jack ---
    checks.extend(_headset_checks(state))

    return Diagnosis(checks=checks)


def _input_checks(source: Device, state: devices.SystemState) -> list[Check]:
    checks: list[Check] = []
    card = state.card_for_device(source)

    if source.muted:
        checks.append(Check("input-muted", "Microphone muted", FAIL, f"{devices.device_label(source)} is muted.", "microphone"))
    else:
        checks.append(Check("input-muted", "Microphone muted", OK, "The microphone is not muted."))

    if card is not None:
        controls = devices.capture_controls(card)
        cap_switch = controls.get("capture_switch")
        if cap_switch is not None:
            cur = cap_switch.current
            is_on = str(cur).lower() == "on" if isinstance(cur, str) else bool(cur)
            checks.append(
                Check(
                    "capture-switch",
                    "Microphone hardware switch",
                    OK if is_on else FAIL,
                    "The hardware microphone input is " + ("enabled." if is_on else "switched off."),
                    None if is_on else "microphone",
                )
            )
        cap_volume = controls.get("capture_volume")
        if cap_volume is not None:
            vol = alsa.element_pct(cap_volume)
            if vol == 0:
                checks.append(Check("capture-volume", "Microphone level", FAIL, "Hardware capture level is zero.", "microphone"))
            elif vol is not None and vol < 20:
                checks.append(Check("capture-volume", "Microphone level", WARN, f"Hardware capture level is {vol}% (may be too quiet).", "microphone"))
            else:
                checks.append(Check("capture-volume", "Microphone level", OK, f"Hardware capture level is {vol}%."))

        boost = devices.mic_boost_info(card)
        if boost is not None:
            db = boost["current_db"]
            if db is not None and db > 20:
                checks.append(
                    Check("mic-boost", "Microphone boost", WARN, f"Microphone boost is very high ({db:g} dB). This may add noise.", None)
                )
            elif db is not None and db < 10:
                checks.append(
                    Check("mic-boost", "Microphone boost", INFO, f"Hardware microphone boost is {db:g} dB.", None)
                )
            else:
                checks.append(
                    Check("mic-boost", "Microphone boost", OK if db is not None else INFO, f"Microphone boost is {db:g} dB.", None)
                )
    return checks


def _headset_checks(state: devices.SystemState) -> list[Check]:
    checks: list[Check] = []
    for card in state.cards:
        pins = devices.jack_pins(card)
        present = {p["name"]: p["present"] for p in pins}
        headphones = present.get("Headphone Jack")
        mic_jack = present.get("Mic Jack")
        if headphones is None and mic_jack is None and not pins:
            continue
        if headphones is True:
            checks.append(Check("headphone-jack", "Headphones", OK, "Headphones are plugged in."))
        elif headphones is False:
            checks.append(Check("headphone-jack", "Headphones", INFO, "No headphones are connected."))
        if mic_jack is True:
            checks.append(Check("headset-mic", "Headset microphone", OK, "A headset microphone connection is detected."))
        elif mic_jack is False:
            checks.append(Check("headset-mic", "Headset microphone", INFO, "No headset microphone is connected."))

        # A plugged headphone/mic jack that the active audio mode ignores.
        if headphones is True or mic_jack is True:
            input_profiles = card.input_profiles()
            if not input_profiles:
                checks.append(
                    Check(
                        "headset-mode",
                        "Audio mode",
                        FAIL,
                        "Listening for headset input needs an audio mode with a microphone. None is available.",
                        "microphone",
                    )
                )
            elif not card.active_profile or card.profile(card.active_profile) is None or not card.profile(card.active_profile).has_input:
                checks.append(
                    Check(
                        "headset-mode",
                        "Audio mode",
                        WARN,
                        "The active audio mode does not use the headset microphone.",
                        "microphone",
                    )
                )
    return checks


def debug_report() -> str:
    """Build a plain-text report for support and debugging.

    Covers the app version, configuration, tools, hardware, and the latest
    diagnosis. The Advanced page shows it and lets the user copy it.
    """
    from .. import __version__

    lines: list[str] = []
    lines.append(f"audalis {__version__}")
    lines.append(f"Config file: {config.CONFIG_FILE}")
    lines.append(f"Log file: {config.LOG_FILE}")
    cfg = config.load()
    startup = (cfg.get("autostart") or {}).get("preset", "")
    lines.append(f"Login restore: {startup or 'off'}")
    lines.append(f"Saved profiles: {len(cfg.get('presets') or {})}")
    from .util import is_available_on_path

    lines.append(
        "Tools: pactl=" + ("yes" if is_available_on_path("pactl") else "no")
        + ", amixer=" + ("yes" if is_available_on_path("amixer") else "no")
        + ", parec=" + ("yes" if is_available_on_path("parec") else "no")
    )

    state = devices.read_system()
    if state.issues:
        lines.append("Hardware issues:")
        for issue in state.issues:
            lines.append(f"- {issue}")
    for card in state.cards:
        lines.append(f"Card: {card.description} (alsa {card.alsa_card_name or card.alsa_card or '?'})")
        lines.append(f"  Active mode: {card.active_profile}")
    for d in state.outputs:
        lines.append(f"Output: {devices.device_label(d)} [{d.name}] port={d.active_port} vol={d.volume_pct}% muted={d.muted}")
    for d in state.inputs:
        lines.append(f"Input: {devices.device_label(d)} [{d.name}] port={d.active_port} vol={d.volume_pct}% muted={d.muted}")

    diag = run_diagnosis()
    lines.append("Diagnosis:")
    for c in diag.checks:
        lines.append(f"  [{c.status}] {c.title}: {c.detail}")
    return "\n".join(lines)