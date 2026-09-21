"""Device model: the human-facing view of the audio hardware.

Translates PipeWire/ALSA naming into concepts ordinary users understand
(Speakers, Headphones, Headset microphone, HDMI, ...) and gathers all
hardware into one picture.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Optional

from .audio import alsa, pulse
from .audio.base import Card, Device, Port

PORT_LABELS = {
    "analog-output": "Analog",
    "analog-output-speaker": "Speakers",
    "analog-output-headphones": "Headphones",
    "analog-output-headset": "Headset",
    "analog-output-lineout": "Line out",
    "analog-input": "Analog input",
    "analog-input-mic": "Microphone jack",
    "analog-input-mic+headphones": "Headset jack",
    "analog-input-linein": "Line in",
    "analog-input-tvtuner": "TV tuner",
    "hdmi-output-0": "HDMI 1",
    "hdmi-output-1": "HDMI 2",
    "hdmi-output-2": "HDMI 3",
    "hdmi-output-3": "HDMI 4",
    "hdmi-output-4": "HDMI 5",
    "hdmi-output-5": "HDMI 6",
    "bluetooth-output": "Bluetooth",
    "bluetooth-input": "Bluetooth",
    "usb-output": "USB",
    "usb-input": "USB",
}

RECORDING_ROLE_MARKERS = ("mic", "headset", "headphone", "webcam", "bluetooth")
OUTPUT_ROLE_MARKERS = ("headphone", "headset", "speaker")


@dataclasses.dataclass
class SystemState:
    cards: list[Card] = dataclasses.field(default_factory=list)
    sinks: list[Device] = dataclasses.field(default_factory=list)
    sources: list[Device] = dataclasses.field(default_factory=list)
    default_sink: str = ""
    default_source: str = ""
    issues: list[str] = dataclasses.field(default_factory=list)

    @property
    def outputs(self) -> list[Device]:
        return [d for d in self.sinks if not d.is_monitor]

    @property
    def inputs(self) -> list[Device]:
        return [d for d in self.sources if not d.is_monitor]

    def default_output(self) -> Optional[Device]:
        return next((d for d in self.outputs if d.name == self.default_sink), None)

    def default_input(self) -> Optional[Device]:
        return next((d for d in self.inputs if d.name == self.default_source), None)

    def card_for_device(self, device: Device) -> Optional[Card]:
        for c in self.cards:
            if c.name == device.alsa_card or (c.alsa_card and c.alsa_card == device.alsa_card):
                return c
        return None


def port_label(port: Port) -> str:
    return PORT_LABELS.get(port.name, port.name[:32])


def is_headphone_port(port: Port) -> bool:
    return "headphone" in port.name or "headset" in port.name or "hp" == port.name


def is_headset_port(port: Port) -> bool:
    return "headset" in port.name or "mic+headphones" in port.name or "headset-mic" in port.name


def is_mic_port(port: Port) -> bool:
    return "mic" in port.name or "headset" in port.name


def _props_lower(device: Device) -> str:
    return " ".join(device.properties.values()).lower()


def device_role(device: Device) -> str:
    """Return a coarse human role for a device: speakers/headphones/headset/hdmi/bt/usb/other."""
    if "bluetooth" in _props_lower(device):
        return "bluetooth"
    if "usb" in _props_lower(device):
        return "usb"
    ports = device.available_ports() or device.ports
    for p in ports:
        pn = p.name.lower()
        if "hdmi" in pn:
            return "hdmi"
        if "headset" in pn:
            return "headset"
        if "headphone" in pn or pn == "analog-output-headphones":
            return "headphones"
        if "speaker" in pn:
            return "speakers"
        if "mic" in pn:
            return "mic"
        if "linein" in pn:
            return "line-in"
        if "lineout" in pn:
            return "line-out"
    return "other"


def device_label(device: Device) -> str:
    """A concise, human title for an output or input device."""
    role = device_role(device)
    base = device.description
    clean = re.sub(r"\s*Analog Stereo\s*$", "", base)
    clean = re.sub(r"\s*Digital Stereo\b.*$", "", clean)
    clean = re.sub(r"\s*\(HDMI.*$", "", clean)
    clean = clean.strip() or base

    vendor = device.properties.get("device.vendor.name", "")
    product = device.properties.get("device.product.name", "")

    if role == "bluetooth":
        product = product or (clean.split()[0] if clean else "Bluetooth device")
        return product
    if role == "usb":
        return f"USB {product or clean}" if product else clean
    if device.direction == "source":
        return _input_label(device, clean)
    return _output_label(device, clean, role)


def _output_label(device: Device, clean: str, role: str) -> str:
    if role == "headphones":
        return "Headphones"
    if role == "headset":
        return "Headset"
    if any("hdmi" in p.name.lower() for p in device.available_ports()):
        active = device.active() or (device.ports[0] if device.ports else None)
        if active and "hdmi" in active.name.lower():
            return port_label(active)
        return "HDMI"
    if "speaker" in clean.lower() or "bang & olufsen" in clean.lower():
        return "Speakers"
    if clean:
        return clean
    return "Output device"


def _input_label(device: Device, clean: str) -> str:
    active = device.active()
    if active and is_headset_port(active):
        return "Headset microphone"
    if active and is_mic_port(active):
        mlabel = port_label(active)
        if "built-in" in clean.lower() or "built in" in clean.lower():
            return "Built-in Microphone"
        return mlabel
    if "built-in" in clean.lower() or "built in" in clean.lower():
        return "Built-in Microphone"
    if "mic" in clean.lower():
        return clean
    return clean or "Microphone"


def connection_label(device: Device) -> str:
    """Label of the currently active connection/port (e.g. 'Speakers')."""
    active = device.active()
    if not active:
        ports = device.available_ports()
        active = ports[0] if ports else None
    if not active:
        return ""
    return port_label(active)


@dataclasses.dataclass
class OutputOption:
    """One choice in the output device chooser.

    A card can only expose one output sink per active audio mode (analog or
    HDMI, not both), so besides the *live* sink we also offer the card's other
    output modes as virtual choices. Selecting one of those switches the card's
    audio mode, which makes that sink appear.
    """

    key: str
    label: str
    detail: str = ""
    live: Optional[Device] = None
    card_name: str = ""
    card_alsa: str = ""
    profile: str = ""
    active: bool = False

    @property
    def is_live(self) -> bool:
        return self.live is not None


@dataclasses.dataclass
class InputOption:
    key: str
    label: str
    detail: str = ""
    live: Optional[Device] = None
    active: bool = False

    @property
    def is_live(self) -> bool:
        return self.live is not None


def _output_part(profile_name: str) -> str:
    return profile_name.split("+")[0]


def _hdmi_index(profile_name: str) -> int:
    m = re.search(r"-extra(\d+)$", profile_name)
    return int(m.group(1)) + 1 if m else 1


def output_options(state: SystemState) -> list[OutputOption]:
    """Output choices: every real sink, plus other audio modes of each card."""
    opts: list[OutputOption] = []
    for d in state.outputs:
        opts.append(
            OutputOption(
                key=d.name,
                label=device_label(d),
                detail=d.description,
                live=d,
                active=d.name == state.default_sink,
            )
        )
    seen: set[str] = {o.label for o in opts}
    for card in state.cards:
        if not card.alsa_card:
            continue
        active_out = _output_part(card.active_profile)
        for p in card.available_profiles():
            if not p.has_output:
                continue
            out = _output_part(p.name)
            if out == active_out:
                continue
            if out == "output:off" or "pro-audio" in p.name:
                continue
            if "hdmi-stereo" in out:
                label = f"HDMI {_hdmi_index(p.name)}"
            elif "analog" in out:
                label = "Built-in Audio"
            else:
                continue
            if label in seen:
                continue
            seen.add(label)
            opts.append(
                OutputOption(
                    key=f"{card.name}:{p.name}",
                    label=label,
                    detail=card.description,
                    card_name=card.name,
                    card_alsa=card.alsa_card,
                    profile=p.name,
                )
            )
    return opts


def input_options(state: SystemState) -> list[InputOption]:
    """Input choices. HDMI has no microphone on this hardware; inputs come from
    every live source (built-in mic / headset mic), regardless of the active
    output mode."""
    return [
        InputOption(key=d.name, label=device_label(d), detail=d.description, live=d, active=d.name == state.default_source)
        for d in state.inputs
    ]


def select_output(state: SystemState, opt: OutputOption) -> bool:
    if opt.is_live:
        return pulse.set_default_sink(opt.key)
    if not opt.card_name or not opt.profile:
        return False
    ok = pulse.set_card_profile(opt.card_name, opt.profile)
    if ok:
        rerun = read_system()
        for d in rerun.outputs:
            if d.alsa_card == opt.card_alsa:
                pulse.set_default_sink(d.name)
                break
    return ok


def select_input(state: SystemState, opt: InputOption) -> bool:
    if not opt.is_live:
        return False
    return pulse.set_default_source(opt.key)


def read_system() -> SystemState:
    cards = pulse.get_cards()
    sinks = pulse.get_sinks()
    sources = pulse.get_sources()
    info = pulse.get_info()
    state = SystemState(
        cards=cards,
        sinks=sinks,
        sources=sources,
        default_sink=info.get("Default Sink", ""),
        default_source=info.get("Default Source", ""),
        issues=preflight(),
    )
    return state


def preflight() -> list[str]:
    problems: list[str] = []
    from .util import is_available_on_path, run_cmd

    if not is_available_on_path("pactl"):
        problems.append("pactl is not installed on this system")
    else:
        res = run_cmd(["pactl", "info"])
        if not res.ok:
            problems.append("audio server not reachable (" + res.err_text()[:80] + ")")
    if not is_available_on_path("amixer"):
        problems.append("amixer is not installed on this system")
    if not problems:
        cards = pulse.get_cards()
        if not cards:
            problems.append("no audio hardware found")
    return problems


def jack_pins(card: Card) -> list[dict]:
    """Direct hardware jack detection from ALSA (read-only)."""
    if not card.alsa_card:
        return []
    els, err = alsa.get_mixer_elements(card.alsa_card)
    pins = []
    for el in els:
        if alsa.is_jack_pin(el):
            pins.append(
                {
                    "name": el.name,
                    "present": el.current == "on",
                }
            )
    return pins


def mic_boost_info(card: Card) -> Optional[dict]:
    if not card.alsa_card:
        return None
    els, _ = alsa.get_mixer_elements(card.alsa_card)
    for el in els:
        if alsa.is_boost(el) and el.mutable:
            return {
                "element": el,
                "current_db": alsa.boost_current_db(el),
                "values": alsa.boost_values(el),
            }
    return None


def capture_controls(card: Card) -> dict[str, Optional[object]]:
    """Capture Switch / Capture Volume raw state for diagnostics."""
    if not card.alsa_card:
        return {}
    els, _ = alsa.get_mixer_elements(card.alsa_card)
    out: dict[str, Optional[object]] = {"capture_switch": None, "capture_volume": None}
    for el in els:
        if el.name == "Capture Switch":
            out["capture_switch"] = el
        elif el.name == "Capture Volume":
            out["capture_volume"] = el
    return out