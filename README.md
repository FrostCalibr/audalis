# audalis: audio control for Linux

*Complex underneath. Stupidly simple on top.*

audalis is a system-settings-style audio app for Linux. It treats the
underlying PipeWire/PulseAudio/ALSA stack as an implementation detail and
talks to you in normal words: **Speakers**, **Headphones**, **Headset
microphone**, **Microphone boost**. No sink names, source IDs or profile
strings.

It also diagnoses and fixes the annoying hardware problems Linux users hit,
like "headset microphone doesn't work" or "headphones don't switch".

## Install

Requires Python ≥ 3.10 and a running PipeWire/PulseAudio session.

```sh
pip install --break-system-packages -e .          # GUI + CLI
pip install --break-system-packages -e ".[dev]"  # + pytest
```

Dependencies: `PySide6` (GUI), plus `pactl` and `amixer` on your system
(usually shipped with PipeWire/PulseAudio and ALSA utilities).

### Desktop install

One-shot installer that installs the package and registers Audalis as a
desktop application (entry + icon):

```sh
./packaging/install.sh            # user install → ~/.local (no sudo)
./packaging/install.sh --system   # system install → /usr (uses sudo)
./packaging/install.sh --no-pip   # skip the package step (reuse existing install)
```

The installer resolves the actual `audalis` binary path, so the desktop
entry works regardless of prefix. Uninstallation is manual: delete the
files listed at the end of the install output.

## GUI

```sh
audalis
```

Sections:

- **Output**: output device, volume, balance, test sound
- **Input**: microphone, input volume, microphone boost, test microphone
- **Headset**: connection status, automatic switching
- **Applications**: per-application volume and mute
- **Profiles**: save/apply/restore configurations, restore at login
- **Troubleshooting**: guided diagnosis and fixes (microphone, headphones)
- **Advanced**: raw audio modes, connections, hardware controls

## CLI

Same logic, no GUI.

```sh
audioctl devices              # list outputs/inputs in plain language
audioctl diagnose             # health check
audioctl fix microphone       # guided headset-microphone repair
audioctl fix headphones       # route output to a headphone jack
audioctl profile list
audioctl profile save my-setup
audioctl profile apply my-setup
audioctl profile delete my-setup
audioctl restore              # apply the login-restore profile
audioctl login my-setup       # restore this profile automatically at login
```

Use `--verbose` to see raw PipeWire/ALSA identifiers:
`audioctl devices --verbose`.

## Layout

```text
audalis/
├── core/            # backend, no Qt imports
│   ├── audio/       #   pactl (pipewire/pulse) + amixer (ALSA) bindings
│   ├── devices.py   #   human-facing device model
│   ├── profiles.py  #   snapshot / apply / compare
│   ├── diagnostics.py
│   ├── fixes.py     #   guided repairs (honest per-step results)
│   ├── rules.py     #   automatic headset switching (opt-in)
│   └── config.py
├── ui/              # PySide6 interface (thin; all logic in core)
│   ├── main.py      #   window + navigation + polling
│   ├── pages.py     #   pages
│   ├── components.py
│   └── sound.py     #   test tone / mic test
├── cli/audioctl.py
└── tests/           # parser + logic tests using captured real output
```

The old monolithic prototype is preserved (unmodified) at `legacy/`.

## Tests

```sh
python -m pytest
```

Unit tests use captured `pactl`/`amixer` output from a real system so they
run headless. Device-*identity* tests guard against regressions on this
hardware; everything else is independent of the live session.