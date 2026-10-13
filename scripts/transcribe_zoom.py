#!/usr/bin/env python3
"""Локальна транскрипція Zoom m4a (faster-whisper). Без ключів."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

AUDIO = Path(
    "/Users/macbookpro/Documents/Zoom/2026-09-14 21.45.40 Vladislav's Zoom Meeting/audio1332708006.m4a"
)
ROOT = Path(__file__).resolve().parent.parent
WAV = ROOT / "data" / "zoom_2026-09-14.wav"
OUT = ROOT / "data" / "zoom_2026-09-14_transcript.txt"


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav() -> Path:
    WAV.parent.mkdir(parents=True, exist_ok=True)
    if WAV.exists() and WAV.stat().st_size > 1000:
        return WAV
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-i",
        str(AUDIO),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(WAV),
    ]
    subprocess.check_call(cmd)
    return WAV


def main() -> int:
    if not AUDIO.exists():
        print(f"Немає файлу: {AUDIO}", file=sys.stderr)
        return 1
    print("Конвертую в wav…")
    wav = to_wav()
    print("Завантажую модель whisper (small)…")
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8")
    print("Транскрибую…")
    segments, info = model.transcribe(
        str(wav),
        language="uk",
        vad_filter=True,
        beam_size=5,
    )
    lines: list[str] = []
    for seg in segments:
        start = int(seg.start)
        mm, ss = divmod(start, 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {seg.text.strip()}")
        print(lines[-1])
    text = "\n".join(lines).strip() + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(f"\nГотово: {OUT} ({len(text)} символів), мова={info.language}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
