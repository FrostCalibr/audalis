"""Tests for fixes, rules, balance math, and stream parsing."""

from pathlib import Path

from audalis.core import devices, fixes, profiles, rules
from audalis.core.audio import alsa, pulse
from audalis.core.audio.base import Card, Device, MixerElement, Port, Profile

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _card(profiles: tuple[str, ...] = ("output:analog-stereo+input:analog-stereo", "output:hdmi-stereo+input:analog-stereo")):
    def sources(p: str) -> int:
        if "+input:" in p or p.startswith("input:"):
            return 1
        return 0

    return Card(
        name="alsa_card.pci",
        description="Built-in Audio",
        alsa_card="0",
        active_profile=profiles[0],
        profiles=[Profile(name=p, sinks=int(p.startswith("output:")), sources=sources(p)) for p in profiles],
    )


def _source_with_ports():
    return Device(
        name="alsa_input.pci.analog", description="Built-in Audio Analog Stereo",
        active_port="analog-input-mic", direction="source",
        ports=[Port("analog-input-mic", description="", availability="available"),
               Port("analog-input-mic+headphones", availability="not-available")],
    )


def test_choose_input_profile_prefers_current():
    card = _card()
    from audalis.core.fixes import _choose_input_profile
    assert _choose_input_profile(card, card.active_profile).name == "output:analog-stereo+input:analog-stereo"


def test_choose_input_profile_from_output_only():
    card = _card(("output:hdmi-stereo", "input:analog-stereo"))
    from audalis.core.fixes import _choose_input_profile
    assert _choose_input_profile(card, "output:hdmi-stereo").has_input


def test_boost_values_math():
    el = MixerElement(numid="8", iface="MIXER", name="Mic Boost Volume", ekind="INTEGER", min=0, max=3, db_step=10.0, mutable=True, current=3)
    assert alsa.boost_values(el) == ["0 dB", "10 dB", "20 dB", "30 dB"]
    assert alsa.boost_current_db(el) == 30.0
    assert alsa.boost_value_for(el, 1) == 1
    # enum variant (some codecs)
    e2 = MixerElement(numid="9", iface="MIXER", name="Mic Boost Volume", ekind="ENUMERATED", mutable=True, items=["0dB", "10dB", "20dB"], current="2")
    assert alsa.boost_current_db(e2) == 20.0
    assert alsa.boost_value_for(e2, 0) == "0"


def test_set_balance_factors():
    # via monkeypatching run_cmd to capture args
    calls = []

    import audalis.core.util as util

    original = util.run_cmd

    def fake(args, **kw):
        calls.append(list(args))
        from audalis.core.util import CommandResult
        return CommandResult(0, "", "")

    util.run_cmd = fake
    try:
        from audalis.core.audio import pulse as p
        p.run_cmd = fake
        assert pulse.set_balance("sink", "s0", 100, 60, 2)
        assert calls[-1] == ["pactl", "set-sink-volume", "s0", "0%", "60%"]
        assert pulse.set_balance("sink", "s0", -50, 80, 2)
        assert calls[-1] == ["pactl", "set-sink-volume", "s0", "80%", "40%"]
        assert pulse.set_balance("sink", "s0", 0, 50, 2)
        assert calls[-1] == ["pactl", "set-sink-volume", "s0", "50%", "50%"]
    finally:
        util.run_cmd = original
        import audalis.core.audio.pulse as p2

        p2.run_cmd = original


def test_stream_parsing():
    streams = pulse._streams_of("sink-input", "sink-inputs")
    assert streams
    assert all(s.direction == "sink-input" for s in streams)
    assert any(s.app_name for s in streams) or any(s.stream_name for s in streams)
    assert all(0 <= s.volume_pct <= 100 for s in streams)


def test_rule_spec_registry():
    assert "headset-auto-switch" in rules.AVAILABLE_RULES


def test_profile_humanise():
    snap = {"cards": {"c1": {"profile": "output:analog-stereo+input:analog-stereo"}},
            "sinks": {"s1": {"port": "analog-output-speaker"}},
            "sources": {"s2": {"port": "analog-input-mic"}},
            "default_sink": "s1", "default_source": "s2", "mixer": {}}
    pretty = profiles.humanise(snap)
    assert pretty["outputs"]["s1"]["connection"] == "Speakers"
    assert pretty["inputs"]["s2"]["connection"] == "Microphone jack"


def _hdmi_state():
    cards = pulse._cards_from_text(read("pactl_cards.txt"))
    sinks = pulse._devices_from_text(read("pactl_sinks.txt"), "sink")
    sources = [s for s in pulse._devices_from_text(read("pactl_sources.txt"), "source")]
    return devices.SystemState(cards=cards, sinks=sinks, sources=sources, default_sink="alsa_output.pci-0000_00_1f.3.hdmi-stereo")


def test_output_options_show_alternate_modes():
    state = _hdmi_state()
    opts = devices.output_options(state)
    labels = [o.label for o in opts]
    live = [o for o in opts if o.is_live]
    # HDMI active in fixture: HDMI 1 is the live sink, analog offered as alternate audio mode
    assert "HDMI 1" in labels
    assert "Built-in Audio" in labels
    assert live and live[0].label == "HDMI 1" and live[0].active
    virtual = next(o for o in opts if not o.is_live)
    assert virtual.profile == "output:analog-stereo+input:analog-stereo"
    assert virtual.key.startswith(virtual.card_name + ":")


def test_output_options_no_duplicate_entries():
    state = _hdmi_state()
    labels = [o.label for o in devices.output_options(state)]
    assert len(labels) == len(set(labels))


def test_select_output_virtual_switches_profile():
    from unittest.mock import patch

    state = _hdmi_state()
    opts = devices.output_options(state)
    virtual = next(o for o in opts if not o.is_live)
    calls = []

    def fake_profile(card, prof):
        calls.append(("profile", card, prof))
        return True

    def fake_default(sink):
        calls.append(("default", sink))
        return True

    with patch("audalis.core.devices.pulse.set_card_profile", fake_profile), \
         patch("audalis.core.devices.pulse.set_default_sink", fake_default):
        ok = devices.select_output(state, virtual)
    assert ok
    assert ("profile", virtual.card_name, virtual.profile) in calls
    # profile switch triggers default rerun; with no new sink found no default is forced
    assert any(c[0] == "profile" for c in calls)