"""Test tone and microphone test helpers.

Playback tries QtMultimedia first, then falls back to aplay/paplay.
Recording uses parec and plays the result back. The done signals let the UI
announce when a test finishes.
"""

from __future__ import annotations

import array
import math
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from PySide6.QtCore import QUrl, QObject, Signal
from PySide6.QtMultimedia import QSoundEffect

_SAMPLE_RATE = 44100
_REFERENCE = None  # keeps a live QSoundEffect from being garbage collected


class SoundTester(QObject):
    """Plays tones and recorded microphone samples."""

    toneDone = Signal()
    recordDone = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="audalis-sound-"))
        self._tone: Path | None = None
        self._confirm: Path | None = None
        self._rec: Path | None = None

    def _play_file(self, path: Path, on_done=None) -> bool:
        if not path.exists():
            return False
        if QSoundEffect is not None:
            try:
                global _REFERENCE
                effect = QSoundEffect(self)
                effect.setSource(QUrl.fromLocalFile(str(path)))
                effect.setVolume(0.8)
                if on_done:
                    effect.finished.connect(on_done)
                _REFERENCE = effect
                effect.play()
                return True
            except Exception:
                _REFERENCE = None
        exe = shutil.which("aplay") or shutil.which("paplay")
        if exe:
            proc = subprocess.Popen([exe, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if on_done:
                def _wait(p=proc, cb=on_done):
                    p.wait()
                    if p.returncode == 0:
                        cb()
                threading.Thread(target=_wait, daemon=True).start()
            return True
        return False

    def play_tone(self) -> bool:
        if self._tone is None:
            self._tone = self._tmpdir / "audalis-tone.wav"
            self._tone.write_bytes(_sine_frames(440.0, 0.45))
        return self._play_file(self._tone, on_done=self.toneDone.emit)

    def play_confirm(self) -> bool:
        if self._confirm is None:
            self._confirm = self._tmpdir / "audalis-confirm.wav"
            self._write_wav(self._confirm, _sine_frames(880.0, 0.15, volume=0.25), channels=2)
        return self._play_file(self._confirm)

    def mic_available(self) -> bool:
        return bool(shutil.which("parec"))

    def record_and_play(self, seconds: float = 3.0) -> bool:
        """Record from the default microphone, then play it back."""
        if not self.mic_available():
            self.recordDone.emit(False)
            return False
        raw = self._tmpdir / "audalis-mic.raw"
        wav = self._tmpdir / "audalis-mic.wav"
        try:
            with open(raw, "wb") as out:
                subprocess.run(
                    ["parec", "--format=s16le", "--rate", str(_SAMPLE_RATE), "--channels=1", "--device=default"],
                    stdout=out,
                    stderr=subprocess.DEVNULL,
                    timeout=seconds,
                )
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            data = raw.read_bytes()
        except OSError:
            data = b""
        if not data:
            self.recordDone.emit(False)
            return False
        self._write_wav(wav, data, channels=1)
        ok = self._play_file(wav, on_done=lambda: self.recordDone.emit(True))
        if not ok:
            self.recordDone.emit(False)
        return ok

    def _write_wav(self, path: Path, frames: bytes, channels: int) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(frames)


def _sine_frames(freq: float, duration: float, volume: float = 0.35) -> bytes:
    n = int(_SAMPLE_RATE * duration)
    samples = array.array("h")
    for i in range(n):
        v = int(32767 * volume * math.sin(2 * math.pi * freq * i / _SAMPLE_RATE))
        samples.append(v)
        samples.append(v)
    return samples.tobytes()