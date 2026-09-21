"""audioctl: command-line access to the audalis audio logic.

Human-first: device names, connections and fixes are described in plain
language. Put --verbose after the subcommand to see raw PipeWire/ALSA
identifiers, for example: audioctl devices --verbose
"""

from __future__ import annotations

import argparse
import sys

from .. import __version__
from ..core import config, devices, diagnostics, fixes, profiles


def _status_mark(status: str) -> str:
    return {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": " ·  "}.get(status, " ·  ")


def _step_mark(step: fixes.FixStep) -> str:
    return "done" if step.ok else "FAIL"


def cmd_devices(args: argparse.Namespace) -> int:
    state = devices.read_system()
    if state.issues:
        print("Problems found while reading audio state:")
        for issue in state.issues:
            print(f"  ! {issue}")
        return 1
    _print_defaults(state)
    print("Outputs")
    for d in state.outputs:
        _print_device(d, state.default_sink, verbose=args.verbose)
    print("Inputs")
    for d in state.inputs:
        _print_device(d, state.default_source, verbose=args.verbose)
    return 0


def _print_defaults(state: devices.SystemState) -> None:
    out = state.default_output()
    inp = state.default_input()
    print("Default output:   " + (devices.device_label(out) if out else "(none)"))
    print("Default microphone: " + (devices.device_label(inp) if inp else "(none)"))
    print()


def _print_device(d, default_name: str, verbose: bool = False) -> None:
    arrow = " >" if d.name == default_name else "  "
    print(f"{arrow} {devices.device_label(d)}"
          + (f"  [{d.description}]" if verbose else "")
          + f"  vol {d.volume_pct}%"
          + f"  {'muted' if d.muted else '·'}")
    conns = d.ports
    for p in conns:
        avail = "✓" if p.available else ("✗" if p.availability != "unknown" else "?")
        mark = "*" if p.name == d.active_port else " "
        label = devices.port_label(p)
        print(f"       {mark} {label} {avail}" + (f"  [{p.name}]" if verbose else ""))
        if p.name == d.active_port:
            pass


def cmd_diagnose(args: argparse.Namespace) -> int:
    diag = diagnostics.run_diagnosis()
    if not diag.checks:
        print("Could not read audio state.")
        return 1
    for c in diag.checks:
        detail = f": {c.detail}" if c.detail else ""
        print(f"{_status_mark(c.status)} {c.title}{detail}")
    print()
    if diag.all_ok:
        print("Everything looks fine.")
        return 0
    print("Suggested fixes: " + ", ".join(diag.supported_fixes() or ["none (see above)"]))
    return 0 if not any(c.status == "fail" for c in diag.problems) else 1


def cmd_fix(args: argparse.Namespace) -> int:
    steps = fixes.run_fix(args.target)
    ok = True
    for s in steps:
        print(f"{_step_mark(s)} {s.description}" + (f"  ({s.detail})" if s.detail else ""))
        ok = ok and s.ok
    if ok:
        print("\nDone.")
    else:
        print("\nSome steps failed. See the details above.")
    return 0 if ok else 1


def cmd_profile(args: argparse.Namespace) -> int:
    cfg = config.load()
    if args.command == "save":
        if not args.name:
            print("profile save requires a name")
            return 1
        snap = profiles.snapshot_current()
        cfg.setdefault("presets", {})[args.name] = snap
        config.save(cfg)
        print(f"Saved profile '{args.name}'")
        print("  " + profiles.preset_summary(snap))
        return 0
    if args.command == "apply":
        snap = (cfg.get("presets") or {}).get(args.name) if args.name else None
        if not snap:
            print(f"Profile '{args.name}' not found")
            return 1

        def report(ok: bool, desc: str, detail: str) -> None:
            print(f"{'done' if ok else 'FAIL'}  {desc}" + ("" if ok else f"  ({detail})"))

        errors = profiles.apply_preset_step(snap, on_result=report)
        print()
        if errors:
            print(f"Applied '{args.name}' with {len(errors)} problem(s).")
            return 1
        print(f"Applied '{args.name}'.")
        return 0
    if args.command == "delete":
        presets = cfg.get("presets") or {}
        if args.name not in presets:
            print(f"Profile '{args.name}' not found")
            return 1
        del presets[args.name]
        if cfg.get("autostart", {}).get("preset") == args.name:
            cfg["autostart"]["preset"] = ""
            config.write_autostart("")
        config.save(cfg)
        print(f"Deleted profile '{args.name}'.")
        return 0
    if args.command == "list":
        presets = cfg.get("presets") or {}
        if not presets:
            print("No saved profiles.")
            return 0
        startup = cfg.get("autostart", {}).get("preset", "")
        cur = profiles.snapshot_current()
        for name, snap in presets.items():
            mark = "*login" if name == startup else ("matches" if profiles.compare_state(cur, snap) else "      ")
            print(f"{mark:>8}  {name}:  {profiles.preset_summary(snap)}")
        return 0
    if args.command == "show":
        snap = (cfg.get("presets") or {}).get(args.name) if args.name else None
        if not snap:
            print(f"Profile '{args.name}' not found")
            return 1
        pretty = profiles.humanise(snap)
        for k, v in pretty.items():
            print(f"{k}:")
            print(f"  {v}")
        return 0
    return 1


def cmd_restore(args: argparse.Namespace) -> int:
    cfg = config.load()
    name = args.preset or (cfg.get("autostart") or {}).get("preset", "")
    snap = (cfg.get("presets") or {}).get(name)
    if not snap:
        print("no profile configured for restore")
        return 1

    def report(ok: bool, desc: str, detail: str) -> None:
        print(f"{'done' if ok else 'FAIL'}  {desc}" + ("" if ok else f"  ({detail})"))

    errors = profiles.apply_preset_step(snap, on_result=report)
    if errors:
        print(f"restored '{name}' with {len(errors)} problem(s)")
        return 1
    print(f"restored profile: {name}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    cfg = config.load()
    name = args.preset or ""
    cfg["autostart"] = {"preset": name}
    config.save(cfg)
    config.write_autostart(name)
    print(f"Startup restore {'enabled for ' + name if name else 'disabled'}.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="audioctl", description="audalis audio control (CLI)")
    ap.add_argument("--version", action="version", version=f"audalis {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p_dev = sub.add_parser("devices", help="list output and input devices")
    p_dev.set_defaults(func=cmd_devices)

    p_diag = sub.add_parser("diagnose", help="check audio health")
    p_diag.set_defaults(func=cmd_diagnose)

    p_fix = sub.add_parser("fix", help="run a guided fix")
    p_fix.add_argument("target", choices=("microphone", "headphones"))
    p_fix.set_defaults(func=cmd_fix)

    p_prof = sub.add_parser("profile", help="manage saved profiles")
    p_prof.add_argument("command", choices=("list", "save", "apply", "delete", "show"))
    p_prof.add_argument("name", nargs="?")
    p_prof.set_defaults(func=cmd_profile)

    p_rest = sub.add_parser("restore", help="apply the autostart profile")
    p_rest.add_argument("--preset", help="profile name (defaults to the login profile)")
    p_rest.set_defaults(func=cmd_restore)

    p_login = sub.add_parser("login", help="set which profile (if any) restores at login")
    p_login.add_argument("preset", nargs="?")
    p_login.set_defaults(func=cmd_login)

    for p in (p_dev, p_diag, p_fix, p_prof, p_rest, p_login):
        p.add_argument("--verbose", action="store_true", help="show raw device/port names")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())