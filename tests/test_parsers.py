"""Parser and logic unit tests against captured real-system output."""

from pathlib import Path

from audalis.core import devices, diagnostics, profiles
from audalis.core.audio import alsa, pulse

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_amixer_finds_boost_and_capture():
    els = alsa.parse_amixer(read("amixer_contents.txt"))
    by_name = {e.name: e for e in els}

    boost = by_name.get("Mic Boost Volume")
    assert boost is not None
    assert boost.ekind == "INTEGER"
    assert boost.min == 0 and boost.max == 3
    assert boost.db_step == 10.0
    assert boost.mutable
    assert alsa.is_boost(boost)
    assert alsa.boost_current_db(boost) == 30.0
    assert alsa.boost_values(boost) == ["0 dB", "10 dB", "20 dB", "30 dB"]
    assert alsa.boost_value_for(boost, 1) == 1

    switch = by_name.get("Capture Switch")
    assert switch is not None and switch.is_boolean and switch.mutable
    assert switch.current == "on"

    volume = by_name.get("Capture Volume")
    assert volume is not None
    assert alsa.element_pct(volume) == 100


def test_parse_cards():
    blocks = pulse._pactl_blocks(read("pactl_cards.txt"))
    cards = [b for b in blocks if b["kind"] == "Card"]
    assert len(cards) >= 1
    card = pulse._card_from_block(cards[0])
    assert card.alsa_card == "0"
    assert card.active_profile
    assert card.input_profiles()
    assert any("hdmi" in p.name for p in card.profiles)


def test_parse_sinks_sources():
    sinks = pulse._devices_from_text(read("pactl_sinks.txt"), "sink")
    assert sinks
    real = [s for s in sinks if not s.is_monitor]
    assert len(real) == len(sinks)
    assert real[0].channels == [40, 40]
    assert real[0].volume_pct == 40
    assert real[0].active_port

    sources = pulse._devices_from_text(read("pactl_sources.txt"), "source")
    assert any(s.is_monitor for s in sources)
    real_inputs = [s for s in sources if not s.is_monitor]
    assert real_inputs
    assert real_inputs[0].direction == "source"


def test_device_labels():
    sinks = pulse._devices_from_text(read("pactl_sinks.txt"), "sink")
    hdmi = [s for s in sinks if not s.is_monitor][0]
    assert devices.port_label(hdmi.active()) == "HDMI 1"
    assert devices.device_label(hdmi) == "HDMI 1"

    sources = pulse._devices_from_text(read("pactl_sources.txt"), "source")
    mic = [s for s in sources if not s.is_monitor][0]
    assert devices.device_label(mic) == "Built-in Microphone"


def test_snapshot_roundtrip_compare():
    data = {"cards": {}, "sinks": {}, "sources": {}, "default_sink": "a", "default_source": "b", "mixer": {}}
    assert profiles.compare_state(data, data)
    changed = {
        "cards": {},
        "sinks": {},
        "sources": {},
        "default_sink": "x",
        "default_source": "b",
        "mixer": {},
    }
    assert not profiles.compare_state(changed, data)


def test_diagnosis_on_fixture_like_live():
    diag = diagnostics.run_diagnosis()
    # must always produce the core hardware checks
    ids = {c.id for c in diag.checks}
    assert "output-present" in ids
    assert "input-present" in ids