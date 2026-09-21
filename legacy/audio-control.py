#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import tkinter as tk
    from tkinter import ttk, simpledialog
    HAVE_TK = True
except Exception:
    HAVE_TK = False

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "audio-control")
CONFIG_FILE = os.path.join(CONFIG_DIR, "presets.json")
AUTOSTART_DIR = os.path.join(os.path.expanduser("~"), ".config", "autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "audio-control.desktop")
SCRIPT_PATH = os.path.abspath(__file__)

PORT_LABELS = {
    "analog-input-mic": "Mic jack",
    "analog-input-mic+headphones": "Headset combo",
    "analog-output-headphones": "Headphones",
    "analog-output-speaker": "Speakers",
    "hdmi-output-0": "HDMI 1",
    "hdmi-output-1": "HDMI 2",
    "hdmi-output-2": "HDMI 3",
    "hdmi-output-3": "HDMI 4",
}


def run_cmd(args, timeout=6):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as exc:
        return 1, "", str(exc)


def nint(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def pactl_blocks(text):
    blocks = []
    cur = None
    section = None
    for raw in text.split("\n"):
        line = raw.rstrip("\n")
        if "\r" in line:
            line = line.replace("\r", "")
        stripped = line.lstrip("\t")
        if not stripped.strip():
            continue
        tabs = len(line) - len(stripped)
        m = re.match(r"^(Card|Sink|Source|Sink Input|Source Output|Module|Client) #\d+", stripped)
        if tabs == 0 and m:
            cur = {"kind": m.group(1), "props": {}, "sections": {}, "props2": {}}
            blocks.append(cur)
            section = None
            continue
        if cur is None:
            continue
        if tabs == 1:
            if stripped.endswith(":") and re.fullmatch(r"[A-Za-z0-9 ]+:", stripped):
                section = stripped[:-1].strip()
                cur["sections"].setdefault(section, [])
            else:
                section = None
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    cur["props"][k.strip()] = v.strip()
                else:
                    cur["props"][stripped.strip()] = ""
        else:
            if tabs != 2:
                continue
            if section == "Properties":
                if " = " in stripped:
                    k2, v2 = stripped.split(" = ", 1)
                    cur["props2"][k2.strip()] = v2.strip().strip('"')
            elif section:
                cur["sections"][section].append(stripped)
    return blocks


def entry_port(line):
    if ":" not in line:
        return None
    name, rest = line.split(":", 1)
    name = name.strip()
    if not re.fullmatch(r"[a-z0-9._\-+]+", name):
        return None
    av = "not-available" if "not-available" in rest else ("available" if "available" in rest else "unknown")
    t = ""
    m = re.search(r"type: (\w+)", rest)
    if m:
        t = m.group(1)
    desc = re.sub(r"\s*\(.*$", "", rest).strip()
    return {"name": name, "description": desc, "typename": t, "availability": av}


def entry_profile(line):
    m = re.match(r"^(.+?): (.+?) \(\s*sinks: (\d+), sources: (\d+), priority: \d+, available: (yes|no)\)$", line)
    if not m:
        return None
    name, desc, sn, sc, avail = m.groups()
    return {"name": name, "description": desc, "sinks": int(sn), "sources": int(sc), "profile_avail": avail == "yes"}


def _volume_pct(props):
    v = props.get("Volume", "")
    m = re.search(r"\b(\d+)%", v)
    return int(m.group(1)) if m else 0


def get_cards():
    rc, out, err = run_cmd(["pactl", "list", "cards"])
    if rc:
        return []
    cards = []
    for b in pactl_blocks(out):
        if b["kind"] != "Card":
            continue
        profiles = [p for p in (entry_profile(x) for x in b["sections"].get("Profiles", [])) if p]
        ports = [p for p in (entry_port(x) for x in b["sections"].get("Ports", [])) if p]
        cards.append({
            "name": b["props"].get("Name", ""),
            "driver": b["props"].get("Driver", ""),
            "active_profile": b["props"].get("Active Profile", ""),
            "description": b["props2"].get("device.description", b["props"].get("Name", "")),
            "alsa_card": b["props2"].get("alsa.card", ""),
            "alsa_card_name": b["props2"].get("alsa.card_name", ""),
            "profiles": profiles,
            "ports": ports,
        })
    return cards


def get_devices(kind):
    block_kind = {"sinks": "Sink", "sources": "Source"}[kind]
    rc, out, err = run_cmd(["pactl", "list", kind])
    res = []
    if rc:
        return res
    for b in pactl_blocks(out):
        if b["kind"] != block_kind:
            continue
        ports = [p for p in (entry_port(x) for x in b["sections"].get("Ports", [])) if p]
        res.append({
            "name": b["props"].get("Name", ""),
            "description": b["props"].get("Description", ""),
            "active_port": b["props"].get("Active Port", ""),
            "state": b["props"].get("State", ""),
            "muted": b["props"].get("Mute", "no") == "yes",
            "volume_pct": _volume_pct(b["props"]),
            "alsa_card": b["props2"].get("alsa.card", ""),
            "alsa_card_name": b["props2"].get("alsa.card_name", ""),
            "ports": ports,
        })
    return res


def get_mixer_elements(alsa_idx):
    rc, out, err = run_cmd(["amixer", "-c", str(alsa_idx), "contents"])
    if rc:
        return [], err
    return parse_amixer(out), ""


def parse_amixer(text):
    head = re.compile(r"numid=(\d+),iface=(\w+),name='((?:[^'\\]|\\.)*)'(?:,index=(\d+))?")
    elements = []
    cur = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("numid="):
            m = head.match(line)
            if not m:
                continue
            cur = {"numid": m.group(1), "iface": m.group(2), "name": m.group(3),
                   "index": m.group(4) or 0, "ekind": "", "access": "",
                   "min": 0, "max": 0, "step": 1, "values": [], "items": [],
                   "db_min": None, "db_step": None, "mutable": False, "current": 0}
            elements.append(cur)
        elif line.startswith(";") and cur:
            if "; type=" in line:
                tm = re.search(r"type=(\w+)", line)
                if tm:
                    cur["ekind"] = tm.group(1)
                am = re.search(r"access=([\w-]+)", line)
                if am:
                    cur["access"] = am.group(1)
                cur["mutable"] = cur["access"].startswith("rw")
                n = re.search(r"min=(\d+)", line)
                mx = re.search(r"max=(\d+)", line)
                st = re.search(r"step=(\d+)", line)
                if n:
                    cur["min"] = int(n.group(1))
                if mx:
                    cur["max"] = int(mx.group(1))
                if st:
                    cur["step"] = int(st.group(1)) or 1
            elif "; Item #" in line:
                m = re.search(r"Item #\d+ '(.*)'$", line)
                if m:
                    cur["items"].append(m.group(1))
        elif line.startswith("|") and cur:
            m = re.search(r"dBscale-min=(-?[\d.]+)dB,step=(-?[\d.]+)dB", line)
            if m:
                cur["db_min"] = float(m.group(1))
                cur["db_step"] = float(m.group(2))
        elif line.startswith(": values=") and cur:
            vals = [v.strip() for v in line[len(": values="):].split(",")]
            cur["values"] = vals
            if cur["ekind"] in ("BOOLEAN", "ENUMERATED"):
                cur["current"] = vals[0] if vals else ""
            else:
                try:
                    cur["current"] = int(vals[0]) if vals else 0
                except ValueError:
                    cur["current"] = vals[0] if vals else 0
    return elements


def element_display(e):
    name = e.get("name", "?")
    cur = nint(e.get("current"))
    mn = nint(e.get("min"))
    mx = nint(e.get("max"))
    if e.get("ekind") == "INTEGER" and mx > mn:
        pct = round((cur - mn) * 100 / (mx - mn))
        db_min, db_step = e.get("db_min"), e.get("db_step")
        if db_min is not None and db_step is not None:
            db = db_min + (cur - mn) * db_step
            return f"{name}: {pct}% ({db:.1f} dB)", pct
        return f"{name}: {pct}%", pct
    return f"{name}: {e.get('current')}", None


def alsa_index_of(card_name):
    for c in get_cards():
        if c["name"] == card_name:
            return c["alsa_card"]
    return None


def argvalue(spec):
    t = spec.get("type")
    v = spec.get("value")
    if t == "BOOLEAN":
        return "on" if str(v).lower() in ("on", "1", "true") else "off"
    return str(v)


def snapshot_current():
    cards = get_cards()
    sinks = get_devices("sinks")
    sources = get_devices("sources")
    data = {"cards": {}, "sinks": {}, "sources": {}, "default_sink": "", "default_source": "", "mixer": {}}
    rc, info, err = run_cmd(["pactl", "info"])
    dm = re.search(r"Default Sink: (\S+)", info or "")
    dsrc = re.search(r"Default Source: (\S+)", info or "")
    if dm:
        data["default_sink"] = dm.group(1)
    if dsrc:
        data["default_source"] = dsrc.group(1)
    for c in cards:
        data["cards"][c["name"]] = {"profile": c["active_profile"]}
        if c["alsa_card"]:
            els, _ = get_mixer_elements(c["alsa_card"])
            mm = {}
            for e in els:
                if e["mutable"] and not _risky_name(e["name"]):
                    mm[e["numid"]] = {"type": e["ekind"], "name": e["name"], "value": e["current"]}
            if mm:
                data["mixer"][c["name"]] = mm
    for s in sinks:
        if s["name"].endswith(".monitor"):
            continue
        data["sinks"][s["name"]] = {"port": s["active_port"]}
    for s in sources:
        if s["name"].endswith(".monitor"):
            continue
        data["sources"][s["name"]] = {"port": s["active_port"]}
    return data


def _risky_name(n):
    return any(k in n for k in ("IEC958", "Channel Map", "ELD"))


def apply_preset(data, on_result=None):
    errors = []

    def report(ok, cmd, msg):
        if on_result:
            on_result(ok, cmd, msg)
        if not ok:
            errors.append(msg or cmd)

    for card_name, vals in (data.get("cards") or {}).items():
        profile = (vals or {}).get("profile", "")
        if not profile:
            continue
        rc, out, err = run_cmd(["pactl", "set-card-profile", card_name, profile])
        report(rc == 0, f"pactl set-card-profile {card_name} {profile}", (err or out).strip()[:200])
    for name, vals in (data.get("sinks") or {}).items():
        p = (vals or {}).get("port", "")
        if p:
            rc, out, err = run_cmd(["pactl", "set-sink-port", name, p])
            report(rc == 0, f"pactl set-sink-port {name} {p}", (err or out).strip()[:200])
    for name, vals in (data.get("sources") or {}).items():
        p = (vals or {}).get("port", "")
        if p:
            rc, out, err = run_cmd(["pactl", "set-source-port", name, p])
            report(rc == 0, f"pactl set-source-port {name} {p}", (err or out).strip()[:200])
    for card_name, elems in (data.get("mixer") or {}).items():
        ci = alsa_index_of(card_name)
        if ci is None:
            continue
        for numid, spec in (elems or {}).items():
            if spec.get("value") is None:
                continue
            val = argvalue(spec)
            rc, out, err = run_cmd(["amixer", "-c", str(ci), "cset", f"numid={numid}", val])
            report(rc == 0, f"amixer numid={numid} {val}", (err or out).strip()[:200])
    if data.get("default_sink"):
        rc, out, err = run_cmd(["pactl", "set-default-sink", data["default_sink"]])
        report(rc == 0, f"pactl set-default-sink {data['default_sink']}", (err or out).strip()[:200])
    if data.get("default_source"):
        rc, out, err = run_cmd(["pactl", "set-default-source", data["default_source"]])
        report(rc == 0, f"pactl set-default-source {data['default_source']}", (err or out).strip()[:200])
    return errors


def short_port(name):
    return PORT_LABELS.get(name, name[:28])


def preset_summary(snapshot):
    chips = []
    out = [short_port(v.get("port", "")) for v in (snapshot.get("sinks") or {}).values() if v.get("port")]
    if out:
        chips.append("out " + ", ".join(out))
    inc = [short_port(v.get("port", "")) for v in (snapshot.get("sources") or {}).values() if v.get("port") and not v.get("port", "").startswith("output:")]
    if inc:
        chips.append("in " + ", ".join(inc))
    for card_map in (snapshot.get("mixer") or {}).values():
        for spec in (card_map or {}).values():
            n = str(spec.get("name", ""))
            if "Mic Boost" in n and spec.get("type") == "INTEGER":
                try:
                    chips.append("boost " + str(int(spec.get("value", 0)) * 10) + " dB")
                except Exception:
                    pass
            if "Capture Switch" in n and spec.get("type") == "BOOLEAN":
                chips.append("mic " + ("on" if str(spec.get("value")) == "on" else "off"))
    if snapshot.get("default_source"):
        chips.append("default in set")
    return " · ".join(chips) if chips else "no audio state recorded"


def compare_state(cur, snap):
    if cur.get("cards") != snap.get("cards", {}):
        return False
    if cur.get("sinks") != snap.get("sinks", {}):
        return False
    if cur.get("sources") != snap.get("sources", {}):
        return False
    if cur.get("default_sink") != snap.get("default_sink", ""):
        return False
    if cur.get("default_source") != snap.get("default_source", ""):
        return False
    m1 = cur.get("mixer", {})
    m2 = snap.get("mixer", {})
    for k in set(m1) | set(m2):
        if k not in m1 or k not in m2:
            return False
        a, b = m1[k], m2[k]
        for n, spec in a.items():
            other = b.get(n)
            if not other:
                return False
            try:
                if spec.get("type") == "INTEGER":
                    if int(spec.get("value", 0)) != int(other.get("value", 0)):
                        return False
                elif str(spec.get("value")) != str(other.get("value")):
                    return False
            except Exception:
                if str(spec.get("value")) != str(other.get("value")):
                    return False
    return True


def fix_headphone_jack_input():
    msgs = []
    cards = get_cards()
    sources = get_devices("sources")
    if not cards:
        return ["no audio cards found"]
    target = cards[0]
    for s in sources:
        for c in cards:
            if c.get("alsa_card_name") and c["alsa_card_name"] == s.get("alsa_card_name"):
                target = c
                break
        if target["alsa_card_name"]:
            break
    active = target["active_profile"]
    chosen = None
    for p in target["profiles"]:
        if p["name"] == active and p["sources"] > 0:
            chosen = p
            break
    if chosen is None:
        base = active.split("+")[0] if "+" in active else active
        for p in target["profiles"]:
            if p["sources"] > 0 and p["name"].startswith(base + "+input:"):
                chosen = p
                break
    if chosen is None:
        for p in target["profiles"]:
            if p["sources"] > 0 and ("input:analog-stereo" in p["name"] or p["name"].startswith("input:")):
                chosen = p
                break
    if chosen is None:
        for p in target["profiles"]:
            if p["sources"] > 0:
                chosen = p
                break
    if chosen and chosen["name"] != active:
        rc, out, err = run_cmd(["pactl", "set-card-profile", target["name"], chosen["name"]])
        if rc:
            msgs.append(f"profile failed: {(err or out).strip()[:120]}")
        else:
            msgs.append(f"profile -> {chosen['name']}")
            sources = get_devices("sources")
    else:
        msgs.append(f"profile ok ({active})")
    an = next((s for s in sources if "analog" in s["name"]), None)
    if an:
        rc, out, err = run_cmd(["pactl", "set-source-port", an["name"], "analog-input-mic"])
        if rc:
            msgs.append(f"port force failed: {(err or out).strip()[:120]}")
        else:
            msgs.append(f"mic port forced on {an['name']}")
        rc, out, err = run_cmd(["pactl", "set-source-mute", an["name"], "0"])
        if rc:
            msgs.append(f"unmute failed: {(err or out).strip()[:120]}")
        else:
            msgs.append("capture unmuted")
        if target.get("alsa_card"):
            els, _ = get_mixer_elements(target["alsa_card"])
            boost = next((e for e in els if "Mic Boost" in e["name"] and e["mutable"]), None)
            if boost:
                cur = boost["current"]
                if not isinstance(cur, int) or cur < 1:
                    run_cmd(["amixer", "-c", str(target["alsa_card"]), "cset", f"numid={boost['numid']}", "1"])
                    msgs.append("Mic Boost -> 10 dB")
                else:
                    msgs.append(f"Mic Boost stays at {cur * 10} dB")
    else:
        msgs.append("no analog capture source available")
    return msgs


def load_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"autostart": {"enabled": False, "preset": ""}, "presets": {}}


def save_cfg(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def write_autostart(preset):
    if preset:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        content = ("[Desktop Entry]\n"
                   "Type=Application\n"
                   "Name=Audio Control Restore\n"
                   f"Exec=python3 {SCRIPT_PATH} --restore\n"
                   "X-GNOME-Autostart-enabled=true\n")
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as fh:
            fh.write(content)
    else:
        try:
            if os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
        except OSError:
            pass


def preflight():
    problems = []
    if not shutil.which("pactl"):
        problems.append("pactl not found on PATH")
    else:
        rc, out, err = run_cmd(["pactl", "info"])
        if rc:
            problems.append("pactl server not reachable: " + (err or out).strip()[:120])
    if not shutil.which("amixer"):
        problems.append("amixer not found on PATH")
    if not problems:
        rc, out, err = run_cmd(["pactl", "list", "cards"])
        if rc or not pactl_blocks(out):
            problems.append("no audio cards detected by PipeWire")
    return problems


if HAVE_TK:

    class AudioApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Audio Control")
            self.geometry("1000x720")
            self.cfg = load_cfg()
            self.op_log = []
            self.unread_errors = 0
            self.log_win = None
            self._tweaks_dirty = True
            self._els_cache = {}
            self._build()
            problems = preflight()
            self._set_banner(problems)
            self.build_presets()
            self.refresh_tweaks()
            self.after(1500, self.poll)

        def _build(self):
            top = ttk.Frame(self, padding=6)
            top.pack(fill="x")
            ttk.Button(top, text="Refresh", command=self.refresh_all).pack(side="left")
            ttk.Button(top, text="Fix mic-jack input", command=self.do_fix).pack(side="left", padx=6)
            self.log_btn = ttk.Button(top, text="Log", command=self.toggle_log)
            self.log_btn.pack(side="right")

            self.banner = ttk.Label(self, text="", foreground="#b00020", anchor="w", padding=2)
            self.banner.pack(fill="x", padx=6)

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=4, pady=4)
            self.tab_pre = ttk.Frame(self.nb)
            self.tab_twe = ttk.Frame(self.nb)
            self.nb.add(self.tab_pre, text="Presets")
            self.nb.add(self.tab_twe, text="Tweaks")
            self.nb.bind("<<NotebookTabChanged>>", self._on_tab)

            self.pre_canvas = tk.Canvas(self.tab_pre, highlightthickness=0)
            pre_scroll = ttk.Scrollbar(self.tab_pre, orient="vertical", command=self.pre_canvas.yview)
            self.pre_container = ttk.Frame(self.pre_canvas)
            self.pre_container.bind("<Configure>", lambda e: self.pre_canvas.configure(scrollregion=self.pre_canvas.bbox("all")))
            self.pre_canvas.create_window((0, 0), window=self.pre_container, anchor="nw")
            self.pre_canvas.configure(yscrollcommand=pre_scroll.set)
            self.pre_canvas.pack(side="left", fill="both", expand=True)
            pre_scroll.pack(side="right", fill="y")

            save_bar = ttk.Frame(self.tab_pre, padding=6)
            save_bar.pack(fill="x", side="bottom")
            ttk.Button(save_bar, text="Save current state as preset…", command=self.save_preset).pack(side="left")

            self.twe_canvas = tk.Canvas(self.tab_twe, highlightthickness=0)
            twe_scroll = ttk.Scrollbar(self.tab_twe, orient="vertical", command=self.twe_canvas.yview)
            self.twe_container = ttk.Frame(self.twe_canvas)
            self.twe_container.bind("<Configure>", lambda e: self.twe_canvas.configure(scrollregion=self.twe_canvas.bbox("all")))
            self.twe_canvas.create_window((0, 0), window=self.twe_container, anchor="nw")
            self.twe_canvas.configure(yscrollcommand=twe_scroll.set)
            self.twe_canvas.pack(side="left", fill="both", expand=True)
            twe_scroll.pack(side="right", fill="y")

            self.status = ttk.Label(self, text="ready", anchor="w", relief="sunken", padding=3)
            self.status.pack(fill="x", side="bottom")

        def _set_banner(self, problems):
            if problems:
                self.banner.config(text="⚠ " + "  ·  ".join(problems))
            else:
                self.banner.config(text="PulseAudio/PipeWire and ALSA controls detected.")
            if problems:
                self.log("preflight: " + " | ".join(problems), is_err=True)

        def _on_tab(self, _event=None):
            if self.nb.index("current") == 1 and self._tweaks_dirty:
                self.refresh_tweaks()

        def log(self, msg, is_err=False):
            ts = time.strftime("%H:%M:%S")
            self.op_log.append((ts, msg, is_err))
            self.op_log = self.op_log[-500:]
            self.status.config(text=msg, foreground="#b00020" if is_err else "#222222")
            if is_err:
                self.unread_errors += 1
            self._update_log_btn()
            if self.log_win is not None and self.log_win.winfo_exists():
                self._fill_log_win()

        def _cmd(self, args):
            rc, out, err = run_cmd(args)
            msg = " ".join(args)
            if rc:
                detail = (err or out).strip()[:160]
                self.log(msg + ("  → " + detail if detail else ""), is_err=True)
            else:
                self.log(msg)
            return rc

        def _update_log_btn(self):
            t = "Log"
            if self.unread_errors:
                t += f" ⚠×{self.unread_errors}"
            self.log_btn.config(text=t)

        def report_callback_exception(self, exc, val, tb):
            import traceback
            detail = "".join(traceback.format_exception(exc, val, tb))
            self.log(f"exception: {val}", is_err=True)
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(os.path.join(CONFIG_DIR, "audio-control.log"), "a", encoding="utf-8") as fh:
                    fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + detail + "\n")
            except OSError:
                pass

        def toggle_log(self):
            if self.log_win is not None and self.log_win.winfo_exists():
                self.log_win.destroy()
                self.log_win = None
                return
            self.log_win = tk.Toplevel(self)
            self.log_win.title("Audio Control — operation log")
            self.log_win.geometry("720x420")
            self._log_text = tk.Text(self.log_win, state="disabled", wrap="word", font=("monospace", 9))
            sc = ttk.Scrollbar(self.log_win, orient="vertical", command=self._log_text.yview)
            self._log_text.configure(yscrollcommand=sc.set)
            sc.pack(side="right", fill="y")
            self._log_text.pack(fill="both", expand=True)
            self._fill_log_win()
            self.unread_errors = 0
            self._update_log_btn()

        def _fill_log_win(self):
            if self.log_win is None or not self.log_win.winfo_exists():
                return
            self._log_text.config(state="normal")
            self._log_text.delete("1.0", "end")
            for ts, msg, is_err in self.op_log:
                prefix = "ERR " if is_err else " ok "
                self._log_text.insert("end", f"{prefix} {ts}  {msg}\n")
            self._log_text.config(state="disabled")
            self._log_text.see("end")

        def _set_status_ok(self, msg):
            self.status.config(text=msg, foreground="#222222")

        def save_preset(self):
            name = simpledialog.askstring("Save preset", "Name for this configuration:", parent=self)
            if not name:
                return
            name = name.strip()
            if not name:
                return
            try:
                snap = snapshot_current()
            except Exception as exc:
                self.log(f"could not snapshot state: {exc}", is_err=True)
                return
            (self.cfg.setdefault("presets", {}))[name] = snap
            save_cfg(self.cfg)
            self.build_presets()
            self._set_status_ok(f"preset '{name}' saved")

        def delete_preset(self, name):
            if name not in (self.cfg.get("presets") or {}):
                return
            del self.cfg["presets"][name]
            startup = (self.cfg.get("autostart") or {}).get("preset")
            if startup == name:
                self.cfg["autostart"] = {"enabled": False, "preset": ""}
                write_autostart("")
            save_cfg(self.cfg)
            self.build_presets()
            self._set_status_ok(f"preset '{name}' deleted")

        def edit_preset(self, name):
            snap = (self.cfg.get("presets") or {}).get(name)
            if not snap:
                self.log(f"preset '{name}' not found", is_err=True)
                return
            snap = json.loads(json.dumps(snap))
            try:
                live_cards = get_cards()
                live_sinks = get_devices("sinks")
                live_sources = get_devices("sources")
            except Exception:
                live_cards, live_sinks, live_sources = [], [], []
            live_card_map = {c["name"]: c for c in live_cards}
            for cn, cd in (snap.get("cards") or {}).items():
                lc = live_card_map.get(cn)
                cd["_profiles"] = [p["name"] for p in lc.get("profiles", [])] if lc else []
            live_sink_map = {s["name"]: s for s in live_sinks}
            for sn, sd in (snap.get("sinks") or {}).items():
                ls = live_sink_map.get(sn)
                sd["_ports"] = [p["name"] for p in ls.get("ports", [])] if ls else []
            live_src_map = {s["name"]: s for s in live_sources}
            for sn, sd in (snap.get("sources") or {}).items():
                ls = live_src_map.get(sn)
                sd["_ports"] = [p["name"] for p in ls.get("ports", [])] if ls else []
            for card_name, numids in (snap.get("mixer") or {}).items():
                lc = live_card_map.get(card_name)
                if not lc:
                    continue
                els, _ = get_mixer_elements(lc["alsa_card"])
                el_map = {e["numid"]: e for e in els}
                for numid, spec in numids.items():
                    le = el_map.get(numid)
                    spec["_items"] = le.get("items", []) if le else []
            win = tk.Toplevel(self)
            win.title(f"Edit preset — {name}")
            win.geometry("740x600")
            win.transient(self)
            win.grab_set()
            outer = ttk.Frame(win, padding=6)
            outer.pack(fill="both", expand=True)
            canvas = tk.Canvas(outer, highlightthickness=0)
            vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            container = ttk.Frame(canvas)
            container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=container, anchor="nw")
            canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            r = 0
            profile_vars = {}
            sink_port_vars = {}
            source_port_vars = {}
            mixer_vars = {}
            ttk.Label(container, text="Preset name:", font=("TkDefaultFont", 10, "bold")).grid(row=r, column=0, sticky="w", padx=4, pady=4)
            name_var = tk.StringVar(value=name)
            ttk.Entry(container, textvariable=name_var, width=40).grid(row=r, column=1, columnspan=3, sticky="w", padx=4, pady=4)
            r += 1
            cards = snap.get("cards") or {}
            if cards:
                lf = ttk.LabelFrame(container, text="Card profiles", padding=6)
                lf.grid(row=r, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
                cr = 0
                for card_name, card_data in cards.items():
                    ttk.Label(lf, text=card_name).grid(row=cr, column=0, sticky="w", padx=4, pady=2)
                    profiles = card_data.get("_profiles") or []
                    cur_profile = card_data.get("profile", "")
                    prof_var = tk.StringVar(value=cur_profile)
                    if profiles:
                        cb = ttk.Combobox(lf, state="readonly", values=profiles, textvariable=prof_var, width=60)
                        if cur_profile in profiles:
                            cb.set(cur_profile)
                    else:
                        ttk.Label(lf, text=cur_profile, foreground="#666666").grid(row=cr, column=1, sticky="w", padx=4)
                        prof_var = tk.StringVar(value=cur_profile)
                    profile_vars[card_name] = prof_var
                    if profiles:
                        cb.grid(row=cr, column=1, sticky="w", padx=4, pady=2)
                    cr += 1
                r += 1
            sinks = snap.get("sinks") or {}
            if sinks:
                lf = ttk.LabelFrame(container, text="Sink ports", padding=6)
                lf.grid(row=r, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
                sr = 0
                for sink_name, sink_data in sinks.items():
                    ttk.Label(lf, text=sink_name, width=45, anchor="w").grid(row=sr, column=0, sticky="w", padx=4, pady=2)
                    ports = sink_data.get("_ports") or []
                    cur_port = sink_data.get("port", "")
                    port_var = tk.StringVar(value=cur_port)
                    if ports:
                        cb = ttk.Combobox(lf, state="readonly", values=ports, textvariable=port_var, width=40)
                        if cur_port in ports:
                            cb.set(cur_port)
                        cb.grid(row=sr, column=1, sticky="w", padx=4, pady=2)
                    else:
                        ttk.Label(lf, text=cur_port, foreground="#666666").grid(row=sr, column=1, sticky="w", padx=4)
                    sink_port_vars[sink_name] = port_var
                    sr += 1
                r += 1
            sources = snap.get("sources") or {}
            if sources:
                lf = ttk.LabelFrame(container, text="Source ports", padding=6)
                lf.grid(row=r, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
                sr = 0
                for src_name, src_data in sources.items():
                    ttk.Label(lf, text=src_name, width=45, anchor="w").grid(row=sr, column=0, sticky="w", padx=4, pady=2)
                    ports = src_data.get("_ports") or []
                    cur_port = src_data.get("port", "")
                    port_var = tk.StringVar(value=cur_port)
                    if ports:
                        cb = ttk.Combobox(lf, state="readonly", values=ports, textvariable=port_var, width=40)
                        if cur_port in ports:
                            cb.set(cur_port)
                        cb.grid(row=sr, column=1, sticky="w", padx=4, pady=2)
                    else:
                        ttk.Label(lf, text=cur_port, foreground="#666666").grid(row=sr, column=1, sticky="w", padx=4)
                    source_port_vars[src_name] = port_var
                    sr += 1
                r += 1
            lf = ttk.LabelFrame(container, text="Defaults", padding=6)
            lf.grid(row=r, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
            ttk.Label(lf, text="Default sink").grid(row=0, column=0, sticky="w", padx=4, pady=2)
            ds_var = tk.StringVar(value=snap.get("default_sink", ""))
            ttk.Entry(lf, textvariable=ds_var, width=50).grid(row=0, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(lf, text="Default source").grid(row=1, column=0, sticky="w", padx=4, pady=2)
            dsrc_var = tk.StringVar(value=snap.get("default_source", ""))
            ttk.Entry(lf, textvariable=dsrc_var, width=50).grid(row=1, column=1, sticky="w", padx=4, pady=2)
            r += 1
            mixer = snap.get("mixer") or {}
            if mixer:
                lf = ttk.LabelFrame(container, text="Mixer", padding=6)
                lf.grid(row=r, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
                mr = 0
                for card_name, numids in mixer.items():
                    ttk.Label(lf, text=card_name, font=("TkDefaultFont", 9, "bold")).grid(row=mr, column=0, columnspan=3, sticky="w", padx=4, pady=(4, 0))
                    mr += 1
                    for numid, spec in numids.items():
                        mtype = spec.get("type", "")
                        mname = spec.get("name", "")
                        mval = spec.get("value")
                        ttk.Label(lf, text=mname, width=36, anchor="w").grid(row=mr, column=0, sticky="w", padx=12, pady=1)
                        if mtype == "BOOLEAN":
                            bv = tk.BooleanVar(value=(str(mval) == "on"))
                            ttk.Checkbutton(lf, variable=bv).grid(row=mr, column=1, sticky="w", padx=4)
                            mixer_vars[(card_name, numid)] = ("BOOLEAN", bv)
                        elif mtype == "ENUMERATED":
                            items = spec.get("_items") or []
                            ev = tk.StringVar(value=str(mval))
                            if items:
                                cb = ttk.Combobox(lf, state="readonly", values=items, textvariable=ev, width=28)
                                try:
                                    ci = int(mval) if str(mval).isdigit() else 0
                                except Exception:
                                    ci = 0
                                if 0 <= ci < len(items):
                                    cb.current(ci)
                                cb.grid(row=mr, column=1, sticky="w", padx=4)
                            else:
                                ttk.Label(lf, text=str(mval)).grid(row=mr, column=1, sticky="w", padx=4)
                            mixer_vars[(card_name, numid)] = ("ENUMERATED", ev)
                        else:
                            iv = tk.IntVar(value=nint(mval))
                            ttk.Spinbox(lf, from_=0, to=99999, textvariable=iv, width=8).grid(row=mr, column=1, sticky="w", padx=4)
                            mixer_vars[(card_name, numid)] = ("INTEGER", iv)
                        mr += 1
                r += 1
            def _on_save():
                new_name = name_var.get().strip()
                if not new_name:
                    return
                snap["default_sink"] = ds_var.get().strip()
                snap["default_source"] = dsrc_var.get().strip()
                for card_name, prof_var in profile_vars.items():
                    if card_name in snap.get("cards", {}):
                        snap["cards"][card_name]["profile"] = prof_var.get()
                for sink_name, port_var in sink_port_vars.items():
                    if sink_name in snap.get("sinks", {}):
                        snap["sinks"][sink_name]["port"] = port_var.get()
                for src_name, port_var in source_port_vars.items():
                    if src_name in snap.get("sources", {}):
                        snap["sources"][src_name]["port"] = port_var.get()
                for (card_name, numid), (mtype, widget) in mixer_vars.items():
                    if card_name in snap.get("mixer", {}) and numid in snap["mixer"][card_name]:
                        if mtype == "BOOLEAN":
                            snap["mixer"][card_name][numid]["value"] = "on" if widget.get() else "off"
                        elif mtype == "ENUMERATED":
                            snap["mixer"][card_name][numid]["value"] = widget.get()
                        else:
                            snap["mixer"][card_name][numid]["value"] = widget.get()
                presets = self.cfg.setdefault("presets", {})
                if new_name != name:
                    if new_name in presets:
                        return
                    presets.pop(name, None)
                    startup = (self.cfg.get("autostart") or {}).get("preset")
                    if startup == name:
                        self.cfg["autostart"]["preset"] = new_name
                        write_autostart(new_name)
                presets[new_name] = snap
                save_cfg(self.cfg)
                self.build_presets()
                self._set_status_ok(f"preset '{new_name}' saved")
                win.destroy()
            bf = ttk.Frame(container)
            bf.grid(row=r, column=0, columnspan=4, pady=8)
            ttk.Button(bf, text="Save", command=_on_save).pack(side="left", padx=6)
            ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="left", padx=6)

        def apply_preset(self, name):
            snap = (self.cfg.get("presets") or {}).get(name)
            if not snap:
                self.log(f"preset '{name}' not found", is_err=True)
                return
            errors = apply_preset(snap, on_result=lambda ok, cmd, msg: self.log(cmd + (f"  → {msg}" if msg and not ok else ""), is_err=not ok))
            self._refresh_badges()
            self._tweaks_dirty = True
            if errors:
                self.log(f"preset '{name}' applied with {len(errors)} issue(s)", is_err=True)
            else:
                self._set_status_ok(f"preset '{name}' applied")

        def build_presets(self):
            for w in self.pre_container.winfo_children():
                w.destroy()
            self.preset_rows = {}
            presets = self.cfg.get("presets") or {}
            if not presets:
                ttk.Label(self.pre_container, text="No presets yet.\n\n"
                                                   "Tweak anything in the Tweaks tab, then press\n"
                                                   "'Save current state as preset…' below to snapshot it.\n\n"
                                                   "Use the ● radio on a preset to reload it automatically at login.",
                          justify="left", padding=10).pack(anchor="w")
                return
            startup = (self.cfg.get("autostart") or {}).get("preset", "")
            self.startup_var = tk.StringVar(value=startup)
            for name, snap in presets.items():
                frm = ttk.Frame(self.pre_container, padding=6, relief="groove")
                frm.pack(fill="x", padx=6, pady=3)
                rb = ttk.Radiobutton(frm, variable=self.startup_var, value=name, command=self._startup_changed, text="login")
                name_lbl = ttk.Label(frm, text=name, font=("TkDefaultFont", 11, "bold"))
                badge = ttk.Label(frm, text="", foreground="#1a7f37")
                summ = ttk.Label(frm, text=preset_summary(snap), foreground="#666666", font=("TkDefaultFont", 9))
                rb.grid(row=0, column=0, rowspan=2, sticky="n", padx=2, pady=4)
                name_lbl.grid(row=0, column=1, sticky="w")
                badge.grid(row=0, column=2, sticky="w", padx=6)
                summ.grid(row=1, column=1, columnspan=3, sticky="w")
                ttk.Button(frm, text="Apply now", command=lambda n=name: self.apply_preset(n)).grid(row=0, column=4, rowspan=2, padx=10, sticky="nsew")
                ttk.Button(frm, text="Edit", width=5, command=lambda n=name: self.edit_preset(n)).grid(row=0, column=5, rowspan=2, sticky="nsew", padx=(0, 2))
                ttk.Button(frm, text="Del", width=4, command=lambda n=name: self.delete_preset(n)).grid(row=0, column=6, rowspan=2, sticky="nsew")
                self.preset_rows[name] = (rb, badge)
            self._refresh_badges()

        def _startup_changed(self):
            chosen = self.startup_var.get()
            self.cfg["autostart"] = {"enabled": bool(chosen), "preset": chosen or ""}
            save_cfg(self.cfg)
            write_autostart(chosen or "")
            self._set_status_ok(f"{chosen} will be restored at login" if chosen else "startup restore disabled")
            self.build_presets()

        def _refresh_badges(self):
            rows = getattr(self, "preset_rows", {})
            if not rows:
                return
            try:
                cur = snapshot_current()
            except Exception as exc:
                self.log(f"state read failed: {exc}", is_err=True)
                return
            for name, (rb, badge_lbl) in rows.items():
                snap = (self.cfg.get("presets") or {}).get(name)
                if snap and compare_state(cur, snap):
                    badge_lbl.config(text="● matches current", foreground="#1a7f37")
                else:
                    badge_lbl.config(text="", foreground="#1a7f37")

        def do_fix(self):
            msgs = fix_headphone_jack_input()
            for m in msgs:
                self.log("fix: " + m, is_err="failed" in m)
            self._refresh_badges()
            self._tweaks_dirty = True
            if self.nb.index("current") == 1:
                self.refresh_tweaks()

        def refresh_all(self):
            self._refresh_badges()
            self.refresh_tweaks()
            self._set_status_ok("refreshed")

        def poll(self):
            try:
                if self.nb.index("current") == 0:
                    self._refresh_badges()
            except Exception:
                pass
            self.after(2000, self.poll)

        def _elements(self, card):
            if not card.get("alsa_card"):
                return []
            key = card["alsa_card"]
            if key not in self._els_cache:
                self._els_cache[key] = get_mixer_elements(key)[0]
            return self._els_cache[key]

        # ---- Tweaks tab ----

        def refresh_tweaks(self):
            self._tweaks_dirty = False
            for w in self.twe_container.winfo_children():
                w.destroy()
            try:
                cards = get_cards()
                sinks = get_devices("sinks")
                sources = get_devices("sources")
                if not cards:
                    ttk.Label(self.twe_container, text="No audio devices found.", padding=10).pack(anchor="w")
                    return
                self._defaults_section(sinks, sources)
                self._profiles_section(cards, sinks, sources)
                self._volume_section("Outputs", sinks, "sink")
                self._volume_section("Inputs", sources, "source")
                self._extras_section(cards)
                self._jacks_section(cards)
            except Exception as exc:
                self.log(f"tweaks rebuild failed: {exc}", is_err=True)
                self._tweaks_dirty = True

        def _defaults_section(self, sinks, sources):
            lf = ttk.LabelFrame(self.twe_container, text="Defaults", padding=6)
            lf.pack(fill="x", padx=6, pady=4)
            rc, info, err = run_cmd(["pactl", "info"])
            dsnk = dsrc = ""
            if not rc:
                m = re.search(r"Default Sink: (\S+)", info or "")
                if m:
                    dsnk = m.group(1)
                m = re.search(r"Default Source: (\S+)", info or "")
                if m:
                    dsrc = m.group(1)
            self._default_combo(lf, 0, "Default output", sinks, dsnk, "sink")
            self._default_combo(lf, 1, "Default input", sources, dsrc, "source")

        def _default_combo(self, parent, row, label, devices, current, kind):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
            seen = []
            disps = []
            for d in devices:
                if d["name"].endswith(".monitor"):
                    continue
                disp = f"{d['description']}  [{d['name']}]"
                disps.append(disp)
                seen.append((disp, d["name"]))
            cb = ttk.Combobox(parent, state="readonly", values=disps, width=70)
            cb.grid(row=row, column=1, sticky="w", padx=4, pady=2)
            for disp, nm in seen:
                if nm == current:
                    cb.set(disp)
                    break
            cb.bind("<<ComboboxSelected>>", lambda ev, k=kind: self._cmd(["pactl", f"set-default-{k}", seen[cb.current()][1]]))

        def _profiles_section(self, cards, sinks, sources):
            for card in cards:
                lf = ttk.LabelFrame(self.twe_container, text="Profiles & ports — " + card["description"], padding=6)
                lf.pack(fill="x", padx=6, pady=4)
                profiles = card["profiles"]
                names = [p["name"] for p in profiles]
                disps = [f"{p['name']}" + ("   (no input)" if p["sources"] == 0 else "") for p in profiles]
                cb = ttk.Combobox(lf, state="readonly", values=disps, width=80)
                if card["active_profile"] in names:
                    cb.current(names.index(card["active_profile"]))
                ttk.Label(lf, text="Profile").grid(row=0, column=0, sticky="w", padx=4, pady=2)
                cb.grid(row=0, column=1, columnspan=3, sticky="w", padx=4, pady=2)
                cb.bind("<<ComboboxSelected>>", lambda ev, c=card: self._set_profile(c, names[cb.current()]))
                ttk.Label(lf, text=card["name"], foreground="#888888", font=("TkDefaultFont", 8)).grid(row=1, column=0, columnspan=4, sticky="w", padx=4)
                r = 2
                for d in sinks + sources:
                    if d.get("alsa_card_name") != card.get("alsa_card_name"):
                        continue
                    if d["name"].endswith(".monitor"):
                        continue
                    kind = "sink" if d in sinks else "source"
                    for port in d["ports"]:
                        stat = {"available": "✓", "not-available": "✗ not detected", "unknown": "?"}[port["availability"]]
                        ttk.Label(lf, text=f"{PORT_LABELS.get(port['name'], port['name'])} — {port['description']}").grid(row=r, column=0, sticky="w", padx=4)
                        ttk.Label(lf, text=stat, foreground="#1a7f37" if port["availability"] == "available" else "#b00020").grid(row=r, column=1, sticky="w")
                        ttk.Button(lf, text="Force", width=6, command=lambda k=kind, n=d["name"], p=port["name"]: self._force_port(k, n, p)).grid(row=r, column=2, padx=4)
                        r += 1

        def _force_port(self, kind, dev, port):
            rc, out, err = run_cmd(["pactl", f"set-{kind}-port", dev, port])
            if rc:
                self.log(f"force {port} on {dev}: {(err or out).strip()[:160]}", is_err=True)
            else:
                self.log(f"forced {port} on {dev}")
            self._tweaks_dirty = True
            self._refresh_badges()

        def _set_profile(self, card, profile):
            self._cmd(["pactl", "set-card-profile", card["name"], profile])
            self._tweaks_dirty = True
            self._refresh_badges()

        def _volume_section(self, title, devices, kind):
            real = [d for d in devices if not d["name"].endswith(".monitor")]
            if not real:
                return
            lf = ttk.LabelFrame(self.twe_container, text=title, padding=6)
            lf.pack(fill="x", padx=6, pady=4)
            for d in real:
                row = ttk.Frame(lf)
                row.pack(fill="x", padx=4, pady=3)
                ttk.Label(row, text=d["description"], width=34, anchor="w").pack(side="left")
                pct_lbl = ttk.Label(row, text=f"{d['volume_pct']}%", width=5)
                var = tk.IntVar(value=d["volume_pct"])
                sc = tk.Scale(row, from_=0, to=100, resolution=1, orient="horizontal",
                              length=200, showvalue=False, variable=var)
                sc.pack(side="left", padx=6)
                sc.bind("<ButtonRelease-1>", lambda ev, d=d, v=var, l=pct_lbl: self._set_pct(d, kind, v, l))
                mute = tk.BooleanVar(value=d["muted"])
                ttk.Checkbutton(row, text="mute", variable=mute, command=lambda d=d, v=mute: self._toggle_mute(d, kind, v)).pack(side="left", padx=6)
                if kind == "source":
                    self._boost_combo(row, d)
                pct_lbl.pack(side="left")

        def _boost_combo(self, row, source):
            boost_el = None
            for c in get_cards():
                if c.get("alsa_card_name") == source.get("alsa_card_name"):
                    els = self._elements(c)
                    boost_el = next((e for e in els if "Mic Boost" in e["name"] and e["mutable"]), None)
                    break
            if not boost_el:
                return
            items = boost_el["items"] or ["0"]
            cb = ttk.Combobox(row, state="readonly", width=8, values=[f"{i * 10}dB" for i in range(len(items))])
            cur = min(max(nint(boost_el["current"]), 0), len(items) - 1)
            cb.current(cur)
            ttk.Label(row, text="boost").pack(side="left", padx=4)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>",
                    lambda ev, c=cb, e=boost_el: self._cmd(["amixer", "-c", str(source["alsa_card"]), "cset",
                                                            f"numid={e['numid']}", str(c.current())]))

        def _set_pct(self, d, kind, var, lbl):
            val = var.get()
            this = self._cmd(["pactl", f"set-{kind}-volume", d["name"], f"{val}%"])
            if this == 0:
                lbl.config(text=f"{val}%")
                d["volume_pct"] = val

        def _toggle_mute(self, d, kind, var):
            rc = self._cmd(["pactl", f"set-{kind}-mute", d["name"], "toggle"])
            if rc == 0:
                d["muted"] = not d["muted"]

        def _extras_section(self, cards):
            skip_names = {"Mic Boost Volume", "Capture Volume", "Capture Switch",
                          "Playback Channel Map", "Capture Channel Map"}
            for card in cards:
                els = self._elements(card)
                writable = [e for e in els if e["mutable"] and e["name"] not in skip_names and e["ekind"] in ("BOOLEAN", "INTEGER", "ENUMERATED")]
                if not writable:
                    continue
                lf = ttk.LabelFrame(self.twe_container, text="ALSA extras — " + card["description"], padding=6)
                lf.pack(fill="x", padx=6, pady=4)
                r = 0
                for e in writable:
                    self._extra_control(lf, card, e, r)
                    r += 1

        def _extra_control(self, parent, card, e, row):
            lbl, pct = element_display(e)
            if e["ekind"] == "BOOLEAN":
                var = tk.BooleanVar(value=bool(e["current"] == "on"))
                ttk.Label(parent, text=e["name"]).grid(row=row, column=0, sticky="w", padx=4)
                ttk.Checkbutton(parent, variable=var, command=lambda: self._amixer(card, e, "on" if var.get() else "off")).grid(row=row, column=1, sticky="w")
            elif e["ekind"] == "ENUMERATED":
                ttk.Label(parent, text=e["name"]).grid(row=row, column=0, sticky="w", padx=4)
                cb = ttk.Combobox(parent, state="readonly", width=28)
                cb["values"] = e["items"]
                try:
                    ci = int(e["current"]) if str(e["current"]).isdigit() else 0
                except Exception:
                    ci = 0
                if 0 <= ci < len(e["items"]):
                    cb.current(ci)
                cb.grid(row=row, column=1, sticky="w")
                cb.bind("<<ComboboxSelected>>", lambda ev, c=cb: self._amixer(card, e, str(c.current())))
            elif e["ekind"] == "INTEGER":
                top = max(e["min"] + 1, e["max"])
                lab = ttk.Label(parent, text=lbl)
                lab.grid(row=row, column=0, sticky="w", padx=4)
                var = tk.IntVar(value=nint(e["current"]))
                sc = tk.Scale(parent, from_=e["min"], to=top, resolution=e["step"] or 1,
                              orient="horizontal", showvalue=False, length=240, variable=var)
                sc.grid(row=row, column=1, sticky="ew")
                sc.bind("<ButtonRelease-1>", lambda ev, c=e, v=var, l=lab: self._scale_apply(card, c, v, l))

        def _scale_apply(self, card, e, var, lab):
            val = var.get()
            this = self._cmd(["amixer", "-c", str(card["alsa_card"]), "cset", f"numid={e['numid']}", str(val)])
            if this == 0:
                e["current"] = val
                lab.config(text=element_display(e)[0])

        def _amixer(self, card, e, value):
            self._cmd(["amixer", "-c", str(card["alsa_card"]), "cset", f"numid={e['numid']}", value])

        def _jacks_section(self, cards):
            for card in cards:
                els = self._elements(card)
                pins = [e for e in els if not e["mutable"] and e["ekind"] == "BOOLEAN" and "Jack" in e["name"]]
                if not pins:
                    continue
                lf = ttk.LabelFrame(self.twe_container, text="Jack pins / diagnostics — " + card["description"], padding=6)
                lf.pack(fill="x", padx=6, pady=4)
                for e in pins:
                    present = e["current"] == "on"
                    color = "#1a7f37" if present else "#b00020"
                    note = "plugged / active" if present else "nothing detected (port may report 'not available')"
                    ttk.Label(lf, text=f"{e['name']}:  {note}", foreground=color).pack(anchor="w", padx=4, pady=1)


def cmd_info():
    print("== Cards ==")
    for c in get_cards():
        print(f"name: {c['name']}")
        print(f"  description: {c['description']}  alsa_card: {c['alsa_card']}")
        print(f"  active_profile: {c['active_profile']}")
        print("  profiles:")
        for p in c["profiles"]:
            print(f"    {p['name']}  [sources={p['sources']} avail={'yes' if p['profile_avail'] else 'no'}]  {p['description']}")
        print("  ports:")
        for p in c["ports"]:
            print(f"    {p['name']}  {p['description']}  {p['availability']}")
    print("\n== Sinks ==")
    for s in get_devices("sinks"):
        print(f"{s['name']}  {s['description']}  vol={s['volume_pct']}% muted={s['muted']} active={s['active_port']} state={s['state']}")
        for p in s["ports"]:
            print(f"    {p['name']}: {p['description']} [{p['availability']}]")
    print("\n== Sources ==")
    for s in get_devices("sources"):
        print(f"{s['name']}  {s['description']}  vol={s['volume_pct']}% muted={s['muted']} active={s['active_port']} state={s['state']}")
        for p in s["ports"]:
            print(f"    {p['name']}: {p['description']} [{p['availability']}]")
    print("\n== Mixer (hidden ALSA controls) ==")
    for c in get_cards():
        if not c["alsa_card"]:
            continue
        els, err = get_mixer_elements(c["alsa_card"])
        print(f"card {c['alsa_card']} ({c['description']}):")
        if err:
            print("  error:", err)
        for e in els:
            lbl, _ = element_display(e)
            print(f"  numid={e['numid']} type={e['ekind']} writable={e['mutable']} {lbl}"
                  + (f" items={e['items']}" if e["items"] else ""))
    print("\n(empty)")


def cmd_restore():
    cfg = load_cfg()
    auto = cfg.get("autostart") or {}
    preset = auto.get("preset") or ""
    if not preset or preset not in (cfg.get("presets") or {}):
        print("no autostart preset configured")
        return 0
    errors = apply_preset(cfg["presets"][preset], on_result=lambda ok, cmd, msg: print(("ERR " if not ok else "ok  ") + cmd))
    if errors:
        print(f"{len(errors)} issue(s) restoring preset '{preset}'")
        return 1
    print(f"restored preset: {preset}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Audio Control GUI (pactl/amixer)")
    ap.add_argument("--info", action="store_true", help="print parsed state and quit")
    ap.add_argument("--restore", action="store_true", help="apply the autostart preset and quit")
    args = ap.parse_args()
    if args.info:
        cmd_info()
        return
    if args.restore:
        sys.exit(cmd_restore())
    if not HAVE_TK:
        print("tkinter is unavailable. Install the 'tk' package and retry.")
        sys.exit(1)
    app = AudioApp()
    app.mainloop()


if __name__ == "__main__":
    main()