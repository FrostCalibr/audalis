"""Automatic rules engine.

The first useful rule switches output and microphone to a connected headset.
A background thread polls the ALSA jack pins and reacts to changes.

The engine is small and safe. It only reacts to jack changes. It never
re-applies state on unrelated events and never overrides a choice the user
made during the session. It is ON by default and can be turned off.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Callable, Optional

from . import devices, fixes
from .audio import pulse

POLL_INTERVAL = 2.0


@dataclasses.dataclass
class RuleSpec:
    id: str
    name: str


AVAILABLE_RULES: dict[str, RuleSpec] = {
    "headset-auto-switch": RuleSpec(
        "headset-auto-switch",
        "Switch output and microphone to a connected headset",
    )
}


def _jack_present_lookup() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for card in pulse.get_cards():
        for pin in devices.jack_pins(card):
            key = (pin["name"] or "").lower()
            out[key] = pin["present"]
    return out


def _find_headphone_jack(present: dict[str, bool]) -> Optional[bool]:
    for key in ("headphone jack", "front headphone jack"):
        if key in present:
            return present[key]
    return None


def apply_headset_rule(on_result: Optional[Callable[[fixes.FixStep], None]] = None) -> bool:
    """Route output and microphone to a connected headset."""
    steps = fixes.fix_headphones()
    for s in steps:
        if on_result:
            on_result(s)
    return all(s.ok for s in steps)


class RuleWatcher(threading.Thread):
    """Background thread that polls jack state and reacts to changes.

    Polling instead of listening for events because ALSA cards often run with
    the auto-port option off, which means the audio server never announces a
    jack plug. Reading the pins directly is reliable for those cards.
    """

    def __init__(
        self,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        interval: float = POLL_INTERVAL,
    ):
        super().__init__(daemon=True)
        self._enabled = False
        self._stop = threading.Event()
        self._interval = interval
        self._previous: Optional[bool] = None
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_log = on_log

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            if self._enabled:
                try:
                    self._evaluate()
                except Exception as exc:  # noqa: BLE001
                    if not self._stop.is_set() and self.on_log:
                        self.on_log(f"rule watcher error: {exc}")

    def _evaluate(self) -> None:
        present = _find_headphone_jack(_jack_present_lookup())
        if present is None:
            self._previous = None
            return
        changed = self._previous is not None and present != self._previous
        self._previous = present
        if not changed:
            return
        if present:
            if self.on_log:
                self.on_log("Headset connected. Routing audio to it.")
            if self.on_connect:
                self.on_connect()
        else:
            if self.on_log:
                self.on_log("Headset disconnected.")
            if self.on_disconnect:
                self.on_disconnect()