"""Profiles: snapshot the current audio configuration, apply it later.

A profile stores the *human-relevant* state: card profiles, active ports,
default output/input, and the handful of ALSA controls that explain
microphone behaviour (capture switch, capture volume, mic boost).
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from . import devices
from .audio import alsa, pulse
from .audio.base import Card, MixerElement

# Controls whose PRESENCE explains a real user-facing behaviour and that are
# safe to restore. Identified by user-meaning, not by fragile raw names alone.
CAPTURE_LIKE = ("capture switch", "capture volume", "mic boost")
_ADVANCED_MARKERS = ("IEC958", "Channel Map", "ELD")


def _filter_control(el: MixerElement) -> bool:
    name = (el.name or "").lower()
    if alsa.is_advanced_only(name) or name.endswith("channel map"):
        return False
    return el.mutable


def _capture_relevant(el: MixerElement) -> bool:
    name = (el.name or "").lower()
    return _filter_control(el) and any(k in name for k in CAPTURE_LIKE)


def snapshot_current() -> dict:
    state = devices.read_system()
    cards = state.cards
    sinks = state.sinks
    sources = state.sources
    data: dict = {"cards": {}, "sinks": {}, "sources": {}, "default_sink": "", "default_source": "", "mixer": {}}

    data["default_sink"] = state.default_sink
    data["default_source"] = state.default_source

    for c in cards:
        data["cards"][c.name] = {"profile": c.active_profile}
        if c.alsa_card:
            els, _ = alsa.get_mixer_elements(c.alsa_card)
            mm = {}
            for e in els:
                if _capture_relevant(e):
                    mm[e.numid] = {"type": e.ekind, "name": e.name, "value": e.current}
            if mm:
                data["mixer"][c.name] = mm
    for s in sinks:
        if s.is_monitor:
            continue
        data["sinks"][s.name] = {"port": s.active_port}
    for s in sources:
        if s.is_monitor:
            continue
        data["sources"][s.name] = {"port": s.active_port}
    return data


def _argvalue(spec: dict) -> str:
    v = (spec or {}).get("value")
    t = (spec or {}).get("type")
    if t == "BOOLEAN":
        return "on" if str(v).lower() in ("on", "1", "true") else "off"
    return str(v)


def apply_preset_step(data: dict, on_result: Optional[Callable[[bool, str, str], None]] = None) -> list[str]:
    """Apply a snapshot, reporting each command's real result."""
    errors: list[str] = []

    def report(ok: bool, desc: str, detail: str = ""):
        if on_result:
            on_result(ok, desc, detail)
        if not ok:
            errors.append(detail or desc)

    for card_name, vals in (data.get("cards") or {}).items():
        profile = (vals or {}).get("profile", "")
        if not profile:
            continue
        rc = pulse.set_card_profile(card_name, profile)
        report(rc, f"Set {card_name} to audio mode", f"pactl set-card-profile {card_name} {profile}")
    for name, vals in (data.get("sinks") or {}).items():
        port = (vals or {}).get("port", "")
        if port:
            report(pulse.set_port("sink", name, port), f"Restore output connection on {name}", f"pactl set-sink-port {name} {port}")
    for name, vals in (data.get("sources") or {}).items():
        port = (vals or {}).get("port", "")
        if port:
            report(pulse.set_port("source", name, port), f"Restore input connection on {name}", f"pactl set-source-port {name} {port}")
    for card_name, elems in (data.get("mixer") or {}).items():
        card = next((c for c in pulse.get_cards() if c.name == card_name), None)
        if not card or not card.alsa_card:
            continue
        for numid, spec in (elems or {}).items():
            if spec.get("value") is None:
                continue
            val = _argvalue(spec)
            ok = alsa.cset(card.alsa_card, numid, val)
            report(ok, f"Restore hardware control {(spec.get('name') or '')}", f"amixer numid={numid} {val}")
    if data.get("default_sink"):
        report(pulse.set_default_sink(data["default_sink"]), "Restore default output", f"pactl set-default-sink {data['default_sink']}")
    if data.get("default_source"):
        report(pulse.set_default_source(data["default_source"]), "Restore default microphone", f"pactl set-default-source {data['default_source']}")
    return errors


apply_preset = apply_preset_step  # alias for compatibility


def _mixer_equal(a: dict, b: dict) -> bool:
    m1 = a.get("mixer", {})
    m2 = b.get("mixer", {})
    seen = set(m1) | set(m2)
    for card in seen:
        x1, x2 = m1.get(card, {}), m2.get(card, {})
        if set(x1) != set(x2):
            return False
        for numid, spec in x1.items():
            other = x2.get(numid)
            if not other:
                return False
            try:
                if spec.get("type") == "INTEGER":
                    if int(spec.get("value", 0)) != int(other.get("value", 0)):
                        return False
                elif str(spec.get("value")) != str(other.get("value")):
                    return False
            except (TypeError, ValueError):
                if str(spec.get("value")) != str(other.get("value")):
                    return False
    return True


def compare_state(current: dict, snapshot: dict) -> bool:
    for key in ("cards", "sinks", "sources", "default_sink", "default_source"):
        if current.get(key) != snapshot.get(key, ""):
            return False
    return _mixer_equal(current, snapshot)


def preset_summary(snapshot: dict) -> str:
    chips: list[str] = []
    out = [devices.PORT_LABELS.get(v.get("port", ""), v.get("port", "")[:28]) for v in (snapshot.get("sinks") or {}).values() if v.get("port")]
    if out:
        chips.append("out " + ", ".join(out))
    inc = [devices.PORT_LABELS.get(v.get("port", ""), v.get("port", "")[:28]) for v in (snapshot.get("sources") or {}).values() if v.get("port")]
    if inc:
        chips.append("in " + ", ".join(inc))
    for card_map in (snapshot.get("mixer") or {}).values():
        for spec in (card_map or {}).values():
            n = str(spec.get("name", ""))
            if "mic boost" in n.lower() and spec.get("type") == "INTEGER":
                try:
                    chips.append("boost " + str(int(spec.get("value", 0)) * 10) + " dB")
                except (TypeError, ValueError):
                    pass
            if "capture switch" in n.lower() and spec.get("type") == "BOOLEAN":
                chips.append("mic " + ("on" if str(spec.get("value")) == "on" else "off"))
    if snapshot.get("default_sink"):
        chips.append("default out set")
    return " · ".join(chips) if chips else "no audio state recorded"


def _port_label_or_raw(name: str) -> str:
    return devices.PORT_LABELS.get(name, name[:28])


port_label = _port_label_or_raw


def humanise(snapshot: dict) -> dict:
    """Human-readable rendering of a snapshot for CLI/UI display."""
    out: dict = {}
    for name, spec in (snapshot.get("cards") or {}).items():
        out.setdefault("cards", {})[name] = {"audio_mode": (spec or {}).get("profile", "")}
    for name, spec in (snapshot.get("sinks") or {}).items():
        out.setdefault("outputs", {})[name] = {"connection": _port_label_or_raw((spec or {}).get("port", ""))}
    for name, spec in (snapshot.get("sources") or {}).items():
        out.setdefault("inputs", {})[name] = {"connection": _port_label_or_raw((spec or {}).get("port", ""))}
    out["default_output"] = snapshot.get("default_sink", "")
    out["default_input"] = snapshot.get("default_source", "")
    for card, elems in (snapshot.get("mixer") or {}).items():
        for numid, spec in (elems or {}).items():
            out.setdefault("hardware_controls", []).append(
                {"card": card, "name": (spec or {}).get("name", ""), "value": (spec or {}).get("value")}
            )
    return out