"""Fixes: guided, honest repairs for the common hardware problems.

Every step records whether the underlying operation actually succeeded and
whether anything needed changing. The UI must never claim a fix worked if
the command failed.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from . import devices
from .audio import alsa, pulse
from .audio.base import Card, Device, Profile


@dataclasses.dataclass
class FixStep:
    description: str
    ok: bool
    changed: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {"description": self.description, "ok": self.ok, "changed": self.changed, "detail": self.detail}


SUPPORTED_FIXES = ("microphone", "headphones")


def run_fix(name: str) -> list[FixStep]:
    if name == "microphone":
        return fix_microphone()
    if name == "headphones":
        return fix_headphones()
    return [FixStep(f"Unknown fix '{name}'", False)]


def _state_with_analog() -> tuple[devices.SystemState, Optional[Card], Optional[Device], Optional[Device]]:
    state = devices.read_system()
    card: Optional[Card] = None
    an_sink: Optional[Device] = None
    an_source: Optional[Device] = None
    for c in state.cards:
        if c.alsa_card and ("analog" in c.active_profile.lower() or c.input_profiles()):
            card = card or c
    for s in state.outputs:
        if "analog" in s.name and "monitor" not in s.name:
            an_sink = s
    for s in state.inputs:
        if "analog" in s.name and "monitor" not in s.name:
            an_source = s
    return state, card, an_sink, an_source


def _choose_input_profile(card: Card, current: str) -> Optional[Profile]:
    active = card.profile(current) or Profile(current)
    if active.has_input:
        return active
    base = current.split("+")[0] if "+" in current else current
    for p in card.profiles:
        if p.available and p.has_input and p.name.startswith(base + "+input:"):
            return p
    for p in card.profiles:
        if p.available and p.has_input and "analog" in p.name:
            return p
    for p in card.profiles:
        if p.available and p.has_input:
            return p
    return None


def _choose_analog_duplex(card: Card) -> Optional[str]:
    """Best output+input profile with an analog output for headphones."""
    current = card.active_profile
    cands = [
        "output:analog-stereo+input:analog-stereo",
        "output:analog-stereo",
    ]
    for c in cands:
        for p in card.profiles:
            if p.available and p.name == c:
                return c
    if current and "analog" in current:
        return current
    for p in card.profiles:
        if p.available and "analog" in p.name and p.has_output:
            return p.name
    return None


def fix_microphone() -> list[FixStep]:
    steps: list[FixStep] = []
    state, card, an_sink, an_source = _state_with_analog()
    if card is None:
        steps.append(FixStep("Find audio hardware with a microphone", False, detail="no compatible audio card found"))
        return steps

    # 1. audio mode with input
    chosen = _choose_input_profile(card, card.active_profile)
    if chosen is None:
        steps.append(FixStep("Select an audio mode that uses the microphone", False, detail="no input-capable mode available"))
        return steps
    if chosen.name == card.active_profile:
        steps.append(FixStep("Select an audio mode that uses the microphone", True))
    else:
        ok = pulse.set_card_profile(card.name, chosen.name)
        steps.append(
            FixStep(
                "Select an audio mode that uses the microphone",
                ok,
                changed=ok,
                detail=f"pactl set-card-profile {card.name} {chosen.name}" if not ok else chosen.name,
            )
        )
        if ok:
            state, card, an_sink, an_source = _state_with_analog()

    # 2. use the microphone jack (prefer the headset port when headphones+mic
    #    are physically present, otherwise the plain mic jack)
    if an_source is None:
        steps.append(FixStep("Use the microphone jack", False, detail="no analog microphone found"))
        return steps
    mic_ports = [p for p in an_source.ports if devices.is_mic_port(p)]
    if not mic_ports:
        steps.append(FixStep("Use the microphone jack", False, detail="no microphone jack on the device"))
        return steps
    headset_detected = False
    if card and card.alsa_card:
        for pin in devices.jack_pins(card):
            if pin["present"] and "headphone" in pin["name"].lower():
                headset_detected = True
                break
    target = None
    if headset_detected:
        target = next((p for p in mic_ports if devices.is_headset_port(p) and p.available), None)
    if target is None:
        target = next((p for p in mic_ports if p.available and p.name == "analog-input-mic"), None)
    if target is None:
        target = next((p for p in mic_ports if p.available), None)
    if target is None:
        target = next((p for p in mic_ports if p.name == "analog-input-mic"), mic_ports[0])
    if an_source.active_port == target.name:
        steps.append(FixStep("Use the microphone jack", True))
    else:
        ok = pulse.set_port("source", an_source.name, target.name)
        steps.append(
            FixStep(
                "Use the microphone jack",
                ok,
                changed=ok,
                detail=f"pactl set-source-port {an_source.name} {target.name}" if not ok else devices.port_label(target),
            )
        )

    # 3. unmute
    if not an_source.muted:
        steps.append(FixStep("Unmute the microphone", True))
    else:
        ok = pulse.set_mute_state("source", an_source.name, False)
        steps.append(FixStep("Unmute the microphone", ok, changed=ok, detail="pactl set-source-mute 0" if not ok else ""))

    # 4. hardware capture switch
    controls = devices.capture_controls(card)
    cap_switch = controls.get("capture_switch")
    if cap_switch is not None:
        if cap_switch.mutable:
            cur = cap_switch.current
            is_on = str(cur).lower() == "on" if isinstance(cur, str) else bool(cur)
            if is_on:
                steps.append(FixStep("Enable the microphone hardware switch", True))
            else:
                ok = alsa.cset(card.alsa_card, cap_switch.numid, "on")
                steps.append(FixStep("Enable the microphone hardware switch", ok, changed=ok, detail=f"amixer numid={cap_switch.numid} on" if not ok else ""))
        else:
            cur = cap_switch.current
            visible = str(cur).lower() == "on" if isinstance(cur, str) else bool(cur)
            steps.append(FixStep("Microphone hardware switch", True, detail="enabled" if visible else "reply disabled"))

    # 5. reasonable level
    cap_volume = controls.get("capture_volume")
    changed_vol = False
    if cap_volume is not None and cap_volume.mutable:
        pct = alsa.element_pct(cap_volume)
        if pct is not None and pct < 10:
            raw = cap_volume.min + round((cap_volume.max - cap_volume.min) * 0.75)
            ok = alsa.cset(card.alsa_card, cap_volume.numid, raw)
            changed_vol = ok
            steps.append(FixStep("Restore a reasonable microphone level", ok, changed=ok, detail="" if ok else f"amixer numid={cap_volume.numid} {raw}"))
        else:
            steps.append(FixStep("Restore a reasonable microphone level", True))
    else:
        steps.append(FixStep("Restore a reasonable microphone level", True, detail="no adjustable hardware level"))

    if an_source.volume_pct == 0 and not changed_vol:
        ok = pulse.set_volume("source", an_source.name, 65)
        steps.append(FixStep("Raise the software microphone volume", ok, changed=ok, detail="" if ok else "pactl set-source-volume 65%"))

    # 6. boost
    boost = devices.mic_boost_info(card)
    if boost is not None:
        el = boost["element"]
        if el.mutable:
            if alsa.boost_current_db(el) >= 10:
                steps.append(FixStep("Check microphone boost", True, detail=f"{boost['current_db']:g} dB"))
            elif alsa.boost_current_db(el) == 0:
                ok = alsa.cset(card.alsa_card, el.numid, alsa.boost_value_for(el, 1))
                steps.append(FixStep("Raise microphone boost", ok, changed=ok, detail="" if ok else f"amixer numid={el.numid} → 10 dB"))
            else:
                steps.append(FixStep("Check microphone boost", True, detail=f"{boost['current_db']:g} dB"))
        else:
            steps.append(FixStep("Microphone boost", True, detail=f"fixed at {boost['current_db']:g} dB"))

    return steps


def fix_headphones() -> list[FixStep]:
    steps: list[FixStep] = []
    state, card, an_sink, an_source = _state_with_analog()
    if card is None or an_sink is None:
        # The analog output may simply not exist yet because the active audio
        # mode only routes to HDMI/other outputs. Switch to an analog mode first.
        target_mode = _choose_analog_duplex(card) if card is not None else None
        if target_mode is None or target_mode == (card.active_profile if card else ""):
            steps.append(
                FixStep(
                    "Find analog headphone output",
                    False,
                    detail="no analog output device found" if card else "no audio card found",
                )
            )
            return steps
        ok = pulse.set_card_profile(card.name, target_mode)
        steps.append(
            FixStep("Select an audio mode with headphone output", ok, changed=ok, detail="" if ok else f"pactl set-card-profile {card.name} {target_mode}")
        )
        if not ok:
            steps.append(FixStep("Find analog headphone output", False, detail="switching the audio mode failed"))
            return steps
        state, card, an_sink, an_source = _state_with_analog()

    if an_sink is None:
        steps.append(FixStep("Find analog headphone output", False, detail="no analog output device after switching mode"))
        return steps

    # 1. ensure the audio mode still has analog output (may have drifted)
    target_mode = _choose_analog_duplex(card)
    if target_mode is None:
        steps.append(FixStep("Select an audio mode with headphone output", False, detail="no analog output mode available"))
        return steps
    if card.active_profile == target_mode:
        steps.append(FixStep("Select an audio mode with headphone output", True))
    else:
        ok = pulse.set_card_profile(card.name, target_mode)
        steps.append(FixStep("Select an audio mode with headphone output", ok, changed=ok, detail="" if ok else f"pactl set-card-profile {card.name} {target_mode}"))
        if ok:
            state, card, an_sink, an_source = _state_with_analog()

    # 2. switch to the headphone connection
    hp = next((p for p in an_sink.ports if devices.is_headphone_port(p)), None)
    if hp is None:
        steps.append(FixStep("Switch output to the headphone connection", False, detail="no headphone port on the device"))
        return steps
    if an_sink.active_port == hp.name:
        steps.append(FixStep("Switch output to the headphone connection", True))
    else:
        ok = pulse.set_port("sink", an_sink.name, hp.name)
        steps.append(FixStep("Switch output to the headphone connection", ok, changed=ok, detail="" if ok else f"pactl set-sink-port {an_sink.name} {hp.name}"))

    # 3. unmute
    if not an_sink.muted:
        steps.append(FixStep("Unmute the output", True))
    else:
        ok = pulse.set_mute_state("sink", an_sink.name, False)
        steps.append(FixStep("Unmute the output", ok, changed=ok, detail="" if ok else "pactl set-sink-mute 0"))

    # 4. make it the default output
    if an_sink.name == state.default_sink:
        steps.append(FixStep("Make headphones the default output", True))
    else:
        ok = pulse.set_default_sink(an_sink.name)
        steps.append(FixStep("Make headphones the default output", ok, changed=ok, detail="" if ok else "pactl set-default-sink"))

    return steps