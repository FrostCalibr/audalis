"""PipeWire / PulseAudio interaction through pactl.

Parses `pactl list` text into typed models and exposes idempotent commands.
"""

from __future__ import annotations

import re
from typing import Iterator, Optional

from ..util import nint, run_cmd
from .base import AppStream, Card, Device, MixerElement, Port, Profile

_BLOCK_RE = re.compile(r"^(Card|Sink|Source|Sink Input|Source Output|Module|Client) #(\d+)")
_SECTION_RE = re.compile(r"[A-Za-z0-9 ]+:")
_PROP_RE = re.compile(r"^(.+?):\s*(.*)$")
_PROP2_RE = re.compile(r"^(.+?) = (.+)$")

DEVICE_KIND = {"sink": "Sink", "source": "Source"}


def _pactl_blocks(text: str) -> list[dict]:
    """Split `pactl list` plain output into per-object blocks."""
    blocks: list[dict] = []
    cur: Optional[dict] = None
    section: Optional[str] = None
    for raw in text.split("\n"):
        line = raw.replace("\r", "")
        stripped = line.lstrip("\t")
        if not stripped.strip():
            continue
        tabs = len(line) - len(stripped)
        m = _BLOCK_RE.match(stripped)
        if tabs == 0 and m:
            cur = {"kind": m.group(1), "num": m.group(2), "props": {}, "sections": {}, "props2": {}}
            blocks.append(cur)
            section = None
            continue
        if cur is None:
            continue
        if tabs == 1:
            if stripped.endswith(":") and _SECTION_RE.fullmatch(stripped):
                section = stripped[:-1].strip()
                cur["sections"].setdefault(section, [])
            else:
                section = None
                pm = _PROP_RE.match(stripped)
                if pm and pm.group(2) is not None:
                    cur["props"][pm.group(1).strip()] = pm.group(2).strip()
                else:
                    cur["props"][stripped.strip()] = ""
        else:
            if tabs != 2:
                continue
            if section is None and " = " in stripped:
                pm2 = _PROP2_RE.match(stripped)
                if pm2:
                    cur["props2"][pm2.group(1).strip()] = pm2.group(2).strip().strip('"')
            elif section is None and ":" in stripped:
                pm = _PROP_RE.match(stripped)
                if pm:
                    cur["props"][pm.group(1).strip()] = pm.group(2).strip()
            elif section == "Properties":
                pm2 = _PROP2_RE.match(stripped)
                if pm2:
                    cur["props2"][pm2.group(1).strip()] = pm2.group(2).strip().strip('"')
            elif section:
                cur["sections"][section].append(stripped)
    return blocks


def parse_port(line: str) -> Optional[Port]:
    if ":" not in line:
        return None
    name, rest = line.split(":", 1)
    name = name.strip()
    if not re.fullmatch(r"[a-z0-9._\-+]+", name):
        return None
    avail = (
        "not-available"
        if "not-available" in rest
        else ("available" if "available" in rest else "unknown")
    )
    ekind = ""
    m = re.search(r"type: (\w+)", rest)
    if m:
        ekind = m.group(1)
    desc = re.sub(r"\s*\(.*$", "", rest).strip()
    return Port(name=name, description=desc, typename=ekind, availability=avail)


def parse_profile_line(line: str) -> Optional[Profile]:
    m = re.match(
        r"^(.+?): (.+?) \(\s*sinks: (\d+), sources: (\d+), priority: \d+, available: (yes|no)\)$",
        line,
    )
    if not m:
        return None
    name, desc, sn, sc, avail = m.groups()
    return Profile(name=name, description=desc, sinks=int(sn), sources=int(sc), available=avail == "yes")


def _channel_volumes(volume_text: str) -> list[int]:
    return [nint(m) for m in re.findall(r"(\d+)%", volume_text)]


def _volume_pct(channels: list[int]) -> int:
    if not channels:
        return 0
    return round(sum(channels) / len(channels))


def get_cards() -> list[Card]:
    res = run_cmd(["pactl", "list", "cards"])
    if not res.ok:
        return []
    return _cards_from_text(res.stdout)


def _cards_from_text(text: str) -> list[Card]:
    cards: list[Card] = []
    for b in _pactl_blocks(text):
        if b["kind"] != "Card":
            continue
        cards.append(_card_from_block(b))
    return cards


def _card_from_block(b: dict) -> Card:
    profiles = [p for p in (parse_profile_line(x) for x in b["sections"].get("Profiles", [])) if p]
    ports = [p for p in (parse_port(x) for x in b["sections"].get("Ports", [])) if p]
    props2 = b["props2"]
    return Card(
        name=b["props"].get("Name", ""),
        driver=b["props"].get("Driver", ""),
        active_profile=b["props"].get("Active Profile", ""),
        description=props2.get("device.description", b["props"].get("Name", "")),
        alsa_card=props2.get("alsa.card", ""),
        alsa_card_name=props2.get("alsa.card_name", ""),
        vendor_name=props2.get("device.vendor.name", ""),
        product_name=props2.get("device.product.name", ""),
        profiles=profiles,
        ports=ports,
        properties=dict(props2),
    )


def _card_fields(block: dict) -> tuple[str, str, str]:
    return (
        block["props2"].get("alsa.card", ""),
        block["props2"].get("alsa.card_name", ""),
        block["props"].get("Description", ""),
    )


def get_devices(kind: str) -> list[Device]:
    """kind is 'sinks' or 'sources'."""
    direction = {"sinks": "sink", "sources": "source"}[kind]
    res = run_cmd(["pactl", "list", kind])
    if not res.ok:
        return []
    return _devices_from_text(res.stdout, direction)


def _devices_from_text(text: str, direction: str) -> list[Device]:
    block_kind = DEVICE_KIND[direction]
    devices: list[Device] = []
    for b in _pactl_blocks(text):
        if b["kind"] != block_kind:
            continue
        dev = _device_from_block(b, direction)
        if dev is not None:
            devices.append(dev)
    return devices


def _device_from_block(b: dict, direction: str) -> Optional[Device]:
    alsa_card, alsa_card_name, _desc = _card_fields(b)
    props = b["props"]
    if not props.get("Name"):
        return None
    channels = _channel_volumes(props.get("Volume", ""))
    ports = [p for p in (parse_port(x) for x in b["sections"].get("Ports", [])) if p]
    return Device(
        name=props["Name"],
        description=props.get("Description", ""),
        active_port=props.get("Active Port", ""),
        state=props.get("State", ""),
        muted=props.get("Mute", "no") == "yes",
        volume_pct=_volume_pct(channels),
        channels=channels,
        alsa_card=alsa_card,
        alsa_card_name=alsa_card_name,
        direction=direction,
        ports=ports,
        properties=dict(b["props2"]),
    )


def get_sinks() -> list[Device]:
    return get_devices("sinks")


def get_sources() -> list[Device]:
    return get_devices("sources")


def get_info() -> dict[str, str]:
    res = run_cmd(["pactl", "info"])
    info: dict[str, str] = {}
    if not res.ok:
        return info
    for line in res.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def get_default_sink() -> str:
    return get_info().get("Default Sink", "")


def get_default_source() -> str:
    return get_info().get("Default Source", "")


# ---- commands ----

def set_volume(kind: str, name: str, pct: int) -> bool:
    return run_cmd(["pactl", f"set-{kind}-volume", name, f"{int(pct)}%"]).ok


def set_balance(kind: str, name: str, balance: int, base_pct: int, n_channels: int) -> bool:
    """balance in [-100, 100] (negative = left-heavy). All channels scaled from base."""
    if n_channels < 2:
        return set_volume(kind, name, base_pct)
    bal = max(-100, min(100, balance))
    factors = [1.0] * n_channels
    if bal >= 0:
        factors[0] = max(0, 100 - bal) / 100.0
    else:
        factors[1] = max(0, 100 + bal) / 100.0
    vols = [f"{round(base_pct * f)}%" for f in factors]
    return run_cmd(["pactl", f"set-{kind}-volume", name, *vols]).ok


def toggle_mute(kind: str, name: str) -> bool:
    return run_cmd(["pactl", f"set-{kind}-mute", name, "toggle"]).ok


def set_mute_state(kind: str, name: str, muted: bool) -> bool:
    return run_cmd(["pactl", f"set-{kind}-mute", name, "1" if muted else "0"]).ok


def set_port(kind: str, name: str, port: str) -> bool:
    return run_cmd(["pactl", f"set-{kind}-port", name, port]).ok


def set_card_profile(card_name: str, profile: str) -> bool:
    return run_cmd(["pactl", "set-card-profile", card_name, profile]).ok


def set_default_sink(name: str) -> bool:
    return run_cmd(["pactl", "set-default-sink", name]).ok


def set_default_source(name: str) -> bool:
    return run_cmd(["pactl", "set-default-source", name]).ok


# ---- per-application streams ----

def get_app_streams() -> list[AppStream]:
    out: list[AppStream] = []
    out.extend(_streams_of("sink-input", "sink-inputs"))
    out.extend(_streams_of("source-output", "source-outputs"))
    return out


def _streams_of(direction: str, pactl_kind: str) -> list[AppStream]:
    res = run_cmd(["pactl", "list", pactl_kind])
    if not res.ok:
        return []
    streams: list[AppStream] = []
    for b in _pactl_blocks(res.stdout):
        kind = "Sink Input" if direction == "sink-input" else "Source Output"
        if b["kind"] != kind:
            continue
        props = b["props"]
        idx = props.get("Index") or b.get("num")
        if idx is None:
            continue
        channels = _channel_volumes(props.get("Volume", ""))
        target_key = "Sink" if direction == "sink-input" else "Source"
        streams.append(
            AppStream(
                index=idx,
                direction=direction,
                target=props.get(target_key, ""),
                volume_pct=_volume_pct(channels),
                muted=props.get("Mute", "no") == "yes",
                corked=props.get("Corked", "no") == "yes",
                app_name=b["props2"].get("application.name", ""),
                stream_name=b["props2"].get("media.name", ""),
            )
        )
    return streams


def set_stream_volume(direction: str, index: str, pct: int) -> bool:
    verb = "set-sink-input-volume" if direction == "sink-input" else "set-source-output-volume"
    return run_cmd(["pactl", verb, index, f"{int(pct)}%"]).ok


def set_stream_mute(direction: str, index: str, muted: bool) -> bool:
    verb = "set-sink-input-mute" if direction == "sink-input" else "set-source-output-mute"
    return run_cmd(["pactl", verb, index, "1" if muted else "0"]).ok


def subscribe_events() -> Iterator[str]:
    """Yield event lines from `pactl subscribe`. Blocks until the process ends."""
    import subprocess

    try:
        p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True)
    except Exception:
        return
    if p.stdout is None:
        return
    try:
        for line in p.stdout:
            yield line.strip()
    finally:
        try:
            p.wait(timeout=1)
        except Exception:
            p.kill()