"""Base types and the command runner shared by all audio backends."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from typing import Optional

from ..util import CommandResult, run_cmd

__all__ = [
    "AppStream",
    "CommandResult",
    "Card",
    "Device",
    "MixerElement",
    "Port",
    "Profile",
    "run_cmd",
    "Availability",
]

AVAILABLE = "available"
NOT_AVAILABLE = "not-available"
UNKNOWN = "unknown"
Availability = (AVAILABLE, NOT_AVAILABLE, UNKNOWN)

MONITOR_SUFFIX = ".monitor"


@dataclasses.dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def err_text(self) -> str:
        return (self.stderr or self.stdout or "").strip()


@dataclasses.dataclass
class Port:
    name: str
    description: str = ""
    typename: str = ""
    availability: str = UNKNOWN
    direction: str = ""

    @property
    def available(self) -> bool:
        return self.availability == AVAILABLE


@dataclasses.dataclass
class Profile:
    name: str
    description: str = ""
    sinks: int = 0
    sources: int = 0
    available: bool = True

    @property
    def has_input(self) -> bool:
        return self.sources > 0

    @property
    def has_output(self) -> bool:
        return self.sinks > 0


@dataclasses.dataclass
class Device:
    """A sink (output) or source (input / microphone)."""

    name: str
    description: str = ""
    active_port: str = ""
    state: str = ""
    muted: bool = False
    volume_pct: int = 0
    channels: list[int] = dataclasses.field(default_factory=list)
    alsa_card: str = ""
    alsa_card_name: str = ""
    direction: str = ""  # "sink" or "source"
    ports: list[Port] = dataclasses.field(default_factory=list)
    properties: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def is_monitor(self) -> bool:
        return self.name.endswith(MONITOR_SUFFIX)

    @property
    def is_output(self) -> bool:
        return self.direction == "sink"

    @property
    def is_input(self) -> bool:
        return self.direction == "source"

    def active(self) -> Optional[Port]:
        for p in self.ports:
            if p.name == self.active_port:
                return p
        return None

    def available_ports(self) -> list[Port]:
        return [p for p in self.ports if p.available]


@dataclasses.dataclass
class MixerElement:
    numid: str
    iface: str
    name: str
    index: int = 0
    ekind: str = ""
    access: str = ""
    min: int = 0
    max: int = 0
    step: int = 1
    values: list = dataclasses.field(default_factory=list)
    items: list[str] = dataclasses.field(default_factory=list)
    db_min: Optional[float] = None
    db_step: Optional[float] = None
    mutable: bool = False
    current: object = 0

    @property
    def is_boolean(self) -> bool:
        return self.ekind == "BOOLEAN"

    @property
    def is_integer(self) -> bool:
        return self.ekind == "INTEGER"

    @property
    def is_enum(self) -> bool:
        return self.ekind == "ENUMERATED"


@dataclasses.dataclass
class Card:
    name: str
    driver: str = ""
    active_profile: str = ""
    description: str = ""
    alsa_card: str = ""
    alsa_card_name: str = ""
    vendor_name: str = ""
    product_name: str = ""
    profiles: list[Profile] = dataclasses.field(default_factory=list)
    ports: list[Port] = dataclasses.field(default_factory=list)
    properties: dict[str, str] = dataclasses.field(default_factory=dict)

    def profile(self, name: str) -> Optional[Profile]:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def available_profiles(self) -> list[Profile]:
        return [p for p in self.profiles if p.available]

    def input_profiles(self) -> list[Profile]:
        return [p for p in self.available_profiles() if p.has_input]


@dataclasses.dataclass
class AppStream:
    """An application's live audio stream (playing or recording)."""

    index: str
    direction: str  # "sink-input" or "source-output"
    target: str = ""
    volume_pct: int = 0
    muted: bool = False
    corked: bool = False
    app_name: str = ""
    stream_name: str = ""

    @property
    def label(self) -> str:
        if self.app_name:
            return self.app_name
        if self.stream_name:
            return self.stream_name
        return f"Stream #{self.index}"


def require_tools(*names: str) -> list[str]:
    return [n for n in names if shutil.which(n) is None]