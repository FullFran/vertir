"""Self-contained demo: synthesises footage + a transcript with ffmpeg/ImageMagick
and runs the full pipeline (now incl. a b-roll cutaway and a corner logo) to a real
MP4. Proves the engine works end-to-end with zero external footage or ML models.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import transcript as T
from . import probe as P
from . import edit as E
from .pipeline import build_ir, render_doc

PHRASES = [
    "hola esto es un short generado por IA",
    "eh o sea el agente escribe un plan",
    "y el motor lo renderiza de forma determinista",
    "este los subtitulos se resaltan palabra por palabra",
    "um listo asi de facil",
]


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, capture_output=True, text=True)


def _make_media(work: str, seconds: int = 16):
    hero = os.path.join(work, "hero.mp4")
    bgm = os.path.join(work, "bgm.m4a")
    broll = os.path.join(work, "broll.mp4")
    logo = os.path.join(work, "logo.png")
    _ff("-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", hero)
    _ff("-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:a", "aac", bgm)
    _ff("-f", "lavfi", "-i", "mandelbrot=size=1280x720:rate=30", "-t", "5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", broll)
    conv = shutil.which("convert") or shutil.which("magick")
    if conv:
        subprocess.run([conv, "-size", "220x220", "xc:none", "-fill", "#FFE000",
                        "-draw", "circle 110,110 110,14", logo], check=True, capture_output=True, text=True)
    else:  # fallback: a solid yellow square via ffmpeg
        _ff("-f", "lavfi", "-i", "color=c=#FFE000:size=180x180:d=1", "-frames:v", "1", logo)
    return hero, bgm, broll, logo


def run(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    hero, bgm, broll, logo = _make_media(out_dir)
    tx = T.synthetic("hero", PHRASES, word_us=360000, gap_us=50000)

    doc = build_ir(hero, tx, title="demo short", bgm_path=bgm)

    # b-roll cutaway over the speech at ~1.2s-3.0s of the original recording
    baid, basset = P.ingest(broll, "broll1")
    doc["assets"][baid] = basset
    E.add_broll(doc, baid, source_at_us=1_200_000, source_end_us=3_000_000,
                broll_start_us=0, broll_end_us=1_800_000, focus_y=0.5)

    # corner logo / watermark for the whole program
    laid, lasset = P.ingest(logo, "logo")
    doc["assets"][laid] = lasset
    E.add_logo(doc, laid, corner="top-right", scale=0.15, opacity=0.9)

    return render_doc(doc, out_dir)
