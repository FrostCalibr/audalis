"""ALSA mixer interaction through amixer (controls on the physical codec)."""

from __future__ import annotations

import re
from typing import Optional

from ..util import nint, run_cmd
from .base import MixerElement

_HEAD_RE = re.compile(r"numid=(\d+),iface=(\w+),name='((?:[^'\\]|\\.)*)'(?:,index=(\d+))?")
_DB_RE = re.compile(r"dBscale-min=(-?[\d.]+)dB,step=(-?[\d.]+)dB")

# Controls that are meaningless to normal users and should never be touched
# except in Advanced mode.
ADVANCED_ONLY_NAME_MARKERS = ("IEC958", "Channel Map", "ELD")

DEFAULT_BOOST_STEP_DB = 10.0


def parse_amixer(text: str) -> list[MixerElement]:
    elements: list[MixerElement] = []
    cur: Optional[MixerElement] = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("numid="):
            m = _HEAD_RE.match(line)
            if not m:
                continue
            cur = MixerElement(
                numid=m.group(1),
                iface=m.group(2),
                name=m.group(3),
                index=nint(m.group(4)),
            )
            elements.append(cur)
        elif line.startswith(";") and cur:
            if "; type=" in line:
                tm = re.search(r"type=(\w+)", line)
                if tm:
                    cur.ekind = tm.group(1)
                am = re.search(r"access=([\w-]+)", line)
                if am:
                    cur.access = am.group(1)
                cur.mutable = bool(cur.access.startswith("rw"))
                n2 = re.search(r"min=(\d+)", line)
                mx = re.search(r"max=(\d+)", line)
                st = re.search(r"step=(\d+)", line)
                if n2:
                    cur.min = int(n2.group(1))
                if mx:
                    cur.max = int(mx.group(1))
                if st:
                    cur.step = int(st.group(1)) or 1
            elif "; Item #" in line:
                m = re.search(r"Item #\d+ '(.*)'$", line)
                if m:
                    cur.items.append(m.group(1))
        elif line.startswith("|") and cur:
            m = _DB_RE.search(line)
            if m:
                cur.db_min = float(m.group(1))
                cur.db_step = float(m.group(2))
        elif line.startswith(": values=") and cur:
            vals = [v.strip() for v in line[len(": values="):].split(",")]
            cur.values = vals
            if cur.ekind in ("BOOLEAN", "ENUMERATED"):
                cur.current = vals[0] if vals else ""
            else:
                try:
                    cur.current = int(vals[0]) if vals else 0
                except ValueError:
                    cur.current = vals[0] if vals else 0
    return elements


def get_mixer_elements(alsa_idx: object) -> tuple[list[MixerElement], str]:
    res = run_cmd(["amixer", "-c", str(alsa_idx), "contents"])
    if not res.ok:
        return [], res.err_text()
    return parse_amixer(res.stdout), ""


def cset(alsa_idx: object, numid: str, value) -> bool:
    res = run_cmd(["amixer", "-c", str(alsa_idx), "cset", f"numid={numid}", str(value)])
    return res.ok


def is_advanced_only(name: str) -> bool:
    return any(k in name for k in ADVANCED_ONLY_NAME_MARKERS)


def is_jack_pin(el: MixerElement) -> bool:
    return not el.mutable and "Jack" in el.name and el.is_boolean


def is_boost(el: MixerElement) -> bool:
    return "Mic Boost" in el.name or "Microphone Boost" in el.name


def boost_step_db(el: MixerElement) -> float:
    """dB per boost step, preferring the element's own dB scale."""
    if el.db_step:
        return float(el.db_step)
    return DEFAULT_BOOST_STEP_DB


def boost_current_db(el: MixerElement) -> float:
    cur = el.current
    if el.is_integer:
        return (int(cur) - el.min) * boost_step_db(el)
    if el.is_enum:
        try:
            idx = int(cur) if str(cur).isdigit() else el.items.index(str(cur))
        except (ValueError, IndexError):
            return 0.0
        return idx * boost_step_db(el)
    return 0.0


def boost_values(el: MixerElement) -> list[str]:
    """Human labels for each boost step, e.g. ['0 dB', '10 dB', '20 dB']."""
    if el.is_enum:
        return [f"{i * boost_step_db(el):g} dB" for i in range(len(el.items))]
    upper = el.max - el.min
    if upper <= 0:
        return ["0 dB"]
    return [f"{i * boost_step_db(el):g} dB" for i in range(upper + 1)]


def boost_value_for(el: MixerElement, pct_index: int) -> object:
    """Raw amixer value corresponding to the step index `pct_index`."""
    pct_index = max(0, min(pct_index, len(boost_values(el)) - 1))
    if el.is_enum:
        return str(pct_index)
    return pct_index + el.min


def display_text(el: MixerElement) -> str:
    name = el.name
    if el.is_boolean:
        return f"{name}: {el.current}"
    if el.is_integer and el.max > el.min:
        cur = nint(el.current)
        db = ""
        if el.db_min is not None and el.db_step:
            db = f" ({el.db_min + (cur - el.min) * el.db_step:.1f} dB)"
        pct = round((cur - el.min) * 100 / (el.max - el.min))
        return f"{name}: {pct}%{db}"
    return f"{name}: {el.current}"


def element_pct(el: MixerElement) -> Optional[int]:
    if el.is_integer and el.max > el.min:
        return round((nint(el.current) - el.min) * 100 / (el.max - el.min))
    return None