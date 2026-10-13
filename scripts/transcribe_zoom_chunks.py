#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("/Users/macbookpro/Desktop/ai_silicon")
CHUNKS = sorted((ROOT / "data" / "zoom_chunks").glob("chunk_*.wav"))
OUT = ROOT / "data" / "zoom_2026-09-14_transcript.txt"
CHUNK_SEC = 180


def main() -> int:
    from faster_whisper import WhisperModel

    print("tiny model…", flush=True)
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    lines: list[str] = []
    for wav in CHUNKS:
        idx = int(wav.stem.split("_")[1])
        offset = idx * CHUNK_SEC
        print(f"chunk {wav.name}", flush=True)
        segments, _ = model.transcribe(str(wav), language="uk", vad_filter=True, beam_size=1)
        for seg in segments:
            t = int(offset + seg.start)
            mm, ss = divmod(t, 60)
            line = f"[{mm:02d}:{ss:02d}] {seg.text.strip()}"
            print(line, flush=True)
            lines.append(line)
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"done {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
