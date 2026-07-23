"""Self-contained demo: synthesises a 16:9 source + bgm with ffmpeg and a
word-level transcript, then runs the full pipeline to a real MP4. Proves the
engine works end-to-end with zero external footage or ML models.
"""
from __future__ import annotations

import os
import subprocess

from . import transcript as T
from .pipeline import build_short

# Phrases become the transcript; some words are fillers the cutter removes,
# and the gaps between phrases become silences it also removes.
PHRASES = [
    "hola esto es un short generado por IA",
    "eh o sea el agente escribe un plan",
    "y el motor lo renderiza de forma determinista",
    "este los subtitulos se resaltan palabra por palabra",
    "um listo asi de facil",
]


def _make_media(work: str, seconds: int = 16) -> tuple[str, str]:
    hero = os.path.join(work, "hero.mp4")
    bgm = os.path.join(work, "bgm.m4a")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", hero,
    ], check=True, capture_output=True, text=True)
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
        "-c:a", "aac", bgm,
    ], check=True, capture_output=True, text=True)
    return hero, bgm


def run(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    hero, bgm = _make_media(out_dir)
    tx = T.synthetic("hero", PHRASES, word_us=360000, gap_us=50000)
    result = build_short(hero, tx, out_dir, title="demo short", bgm_path=bgm)
    return result
