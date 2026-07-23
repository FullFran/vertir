"""Deterministic renderer: IR -> FFmpeg -> MP4.

v1 core slice: main track (filler-cut + reframe) concat, word-highlight captions,
optional bgm mix, delivery-profile encode + loudness normalize. Overlays/titles/
ducking are modeled in the IR but rendered in later slices.

Caption backend is auto-detected so it works across ffmpeg builds:
  * "libass"      -> ffmpeg `subtitles` filter burns generated ASS  (best)
  * "imagemagick" -> render per-word PNGs (pango, real word-highlight) + overlay
  * "none"        -> render without burned captions (warns; IR keeps the data)

`build_command` is pure (returns args + sidecar) so it is unit-testable without
invoking ffmpeg.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from typing import Any

from . import ir as I
from . import edit as E
from . import probe as P


# ------------------------------------------------------------------ small utils
def _us_to_s(us: int) -> str:
    return f"{us / 1_000_000:.6f}"


def _db(gain_db: float) -> str:
    return f"{gain_db:.2f}dB"


def _hex_to_ass(color: str) -> str:
    c = color.lstrip("#")
    if len(c) != 6:
        return "&H00FFFFFF&"
    rr, gg, bb = c[0:2], c[2:4], c[4:6]
    return f"&H00{bb}{gg}{rr}&".upper()


def _ass_time(us: int) -> str:
    cs = us // 10000
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cc = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cc:02d}"


def _sub_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _pango_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def reframe_filter(mode: str, cw: int, ch: int, fx: float, fy: float) -> str:
    if mode == "contain":
        return (f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black")
    return (f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch}:(iw-{cw})*{fx:.4f}:(ih-{ch})*{fy:.4f}")


# ------------------------------------------------------------------ backend detect
@lru_cache(maxsize=1)
def _has_subtitles_filter() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=20)
        return " subtitles " in out.stdout
    except Exception:
        return False


@lru_cache(maxsize=1)
def caption_backend() -> str:
    if _has_subtitles_filter():
        return "libass"
    if shutil.which("convert") or shutil.which("magick"):
        return "imagemagick"
    return "none"


# ------------------------------------------------------------------ captions: ASS (libass)
def generate_ass(ir: dict, events: list[dict], cw: int, ch: int) -> str:
    cap = E.caption_track_of(ir)
    st = cap.get("style", {}) if cap else I.caption_style()
    fill = _hex_to_ass(st.get("fillColor", "#FFFFFF"))
    hi = _hex_to_ass(st.get("highlightColor", "#FFE000"))
    stroke = _hex_to_ass(st.get("strokeColor", "#000000"))
    size = int(st.get("fontSizePx", 76))
    font = st.get("fontFamily", "DejaVu Sans")
    outline = max(1, int(st.get("strokePx", 8) / 2))
    bold = -1 if int(st.get("fontWeight", 800)) >= 600 else 0
    upper = st.get("uppercase", True)
    y = int(ch * st.get("position", {}).get("yPct", 0.74))
    x = cw // 2
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {cw}\nPlayResY: {ch}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
        "BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font},{size},{fill},{stroke},&H64000000&,{bold},{outline},0,5,40,40,40,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def line(words, active):
        parts = []
        for i, w in enumerate(words):
            txt = (w["text"].upper() if upper else w["text"]).replace("{", "(").replace("}", ")")
            parts.append(f"{{\\c{hi}}}{txt}{{\\c{fill}}}" if i == active else txt)
        return " ".join(parts)

    rows = []
    for ev in events:
        for idx, w in enumerate(ev["words"]):
            body = f"{{\\an5\\pos({x},{y})}}" + line(ev["words"], idx)
            rows.append(f"Dialogue: 0,{_ass_time(w['progAtUs'])},{_ass_time(w['progEndUs'])},Cap,,0,0,0,,{body}")
    return header + "\n".join(rows) + "\n"


# ------------------------------------------------------------------ captions: PNG (imagemagick)
def _caption_states(ir: dict, events: list[dict], scale: float) -> list[dict]:
    """One state per active-word window: {startUs, endUs, markup}."""
    cap = E.caption_track_of(ir)
    st = cap.get("style", {}) if cap else I.caption_style()
    family = st.get("fontFamily", "DejaVu Sans")
    fill = st.get("fillColor", "#FFFFFF")
    hi = st.get("highlightColor", "#FFE000")
    size = max(8, int(round(st.get("fontSizePx", 76) * scale)))
    upper = st.get("uppercase", True)
    out = []
    for ev in events:
        words = ev["words"]
        for active, w in enumerate(words):
            spans = []
            for i, ww in enumerate(words):
                txt = _pango_escape(ww["text"].upper() if upper else ww["text"])
                color = hi if i == active else fill
                spans.append(f"<span font='{family} Bold {size}' foreground='{color}'>{txt}</span>")
            out.append({"startUs": w["progAtUs"], "endUs": w["progEndUs"],
                        "markup": "<span> </span>".join(spans)})
    return out


def render_caption_pngs(ir: dict, events: list[dict], work_dir: str,
                        scale: float) -> list[dict]:
    cap = E.caption_track_of(ir)
    st = cap.get("style", {}) if cap else I.caption_style()
    stroke_px = int(st.get("strokePx", 8))
    disk = max(1, int(round(stroke_px / 2 * scale)))
    border = max(2, int(round(12 * scale)))
    conv = shutil.which("convert") or shutil.which("magick")
    caps_dir = os.path.join(work_dir, "caps")
    os.makedirs(caps_dir, exist_ok=True)
    overlays = []
    for k, state in enumerate(_caption_states(ir, events, scale)):
        png = os.path.join(caps_dir, f"cap_{k:04d}.png")
        args = [conv, "-background", "none", "-define", "pango:align=center",
                f"pango:{state['markup']}",
                "(", "+clone", "-channel", "A", "-morphology", "Dilate", f"Disk:{disk}",
                "+channel", "+level-colors", "black", ")",
                "-compose", "DstOver", "-composite",
                "-bordercolor", "none", "-border", str(border), "+repage", png]
        subprocess.run(args, check=True, capture_output=True, text=True)
        overlays.append({"path": png, "startUs": state["startUs"], "endUs": state["endUs"]})
    return overlays


# ------------------------------------------------------------------ command build
def build_command(ir: dict, out_path: str, ass_path: str | None = None,
                  caption_overlays: list[dict] | None = None, proxy: bool = False) -> dict:
    proj = ir["project"]
    cw, ch = proj["canvas"]["w"], proj["canvas"]["h"]
    if proxy:
        cw, ch = cw // 2, ch // 2
    f = proj["fps"]
    fps_r = f"{f['num']}/{f['den']}"
    out = proj.get("output", I.default_output())
    assets = ir["assets"]
    clips = I.main_track(ir)["clips"]

    E.derive(ir)
    events = E.resolve_caption_events(ir)

    broll_windows = E.resolve_broll_windows(ir)
    logo = E.logo_clip_of(ir)

    used: list[str] = []

    def _use(aid: str) -> None:
        if aid not in used:
            used.append(aid)

    for c in clips:
        _use(c["asset"])
    bgm = None
    for t in ir["tracks"]:
        if t.get("kind") == "audio" and t.get("role") == "bgm" and t.get("clips"):
            bgm = t["clips"][0]
            _use(bgm["asset"])
            break
    for bw in broll_windows:
        _use(bw["asset"])
    if logo:
        _use(logo["asset"])
    input_index = {aid: i for i, aid in enumerate(used)}

    args: list[str] = ["ffmpeg", "-y", "-hide_banner", "-nostdin"]
    for aid in used:
        args += ["-i", assets[aid]["path"]]
    cap_base = len(used)
    if caption_overlays:
        for ov in caption_overlays:
            args += ["-i", ov["path"]]

    fc: list[str] = []
    vlabels, alabels = [], []
    for i, c in enumerate(clips):
        idx = input_index[c["asset"]]
        s, e = c["source"]["startUs"], c["source"]["endUs"]
        rf = c.get("reframe", {"mode": "cover", "focusX": 0.5, "focusY": 0.4})
        vf = reframe_filter(rf.get("mode", "cover"), cw, ch, rf.get("focusX", 0.5), rf.get("focusY", 0.4))
        fc.append(f"[{idx}:v]trim=start={_us_to_s(s)}:end={_us_to_s(e)},"
                  f"setpts=PTS-STARTPTS,{vf},fps={fps_r},format=yuv420p[v{i}]")
        has_audio = assets[c["asset"]].get("probe", {}).get("hasAudio", True)
        gain = c.get("audio", {}).get("gainDb", 0.0)
        if has_audio and not c.get("audio", {}).get("mute", False):
            fc.append(f"[{idx}:a]atrim=start={_us_to_s(s)}:end={_us_to_s(e)},"
                      f"asetpts=PTS-STARTPTS,volume={_db(gain)}[a{i}]")
        else:
            fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{_us_to_s(e - s)},asetpts=PTS-STARTPTS[a{i}]")
        vlabels.append(f"[v{i}]")
        alabels.append(f"[a{i}]")

    n = len(clips)
    fc.append("".join(vlabels) + f"concat=n={n}:v=1:a=0[vcat]")
    fc.append("".join(alabels) + f"concat=n={n}:v=0:a=1[speech]")

    # video compositing chain: main -> b-roll -> captions -> logo
    scale_f = cw / proj["canvas"]["w"]
    vin = "[vcat]"
    ass_text = ""

    # b-roll cutaways (source-anchored, under captions; audio stays underneath)
    for k, bw in enumerate(broll_windows):
        idx = input_index[bw["asset"]]
        clip = bw["clip"]
        ps_s, pe_s = _us_to_s(bw["progStartUs"]), _us_to_s(bw["progEndUs"])
        rf = clip.get("reframe", {"mode": "cover", "focusX": 0.5, "focusY": 0.5})
        vf = reframe_filter(rf.get("mode", "cover"), cw, ch, rf.get("focusX", 0.5), rf.get("focusY", 0.5))
        if assets[bw["asset"]].get("kind") == "image":
            fc.append(f"[{idx}:v]{vf},setsar=1,format=yuv420p[bpre{k}]")
        else:
            s, e = _us_to_s(clip["source"]["startUs"]), _us_to_s(clip["source"]["endUs"])
            shift = bw["progStartUs"] / 1_000_000
            fc.append(f"[{idx}:v]trim=start={s}:end={e},setpts=PTS-STARTPTS+{shift:.6f}/TB,"
                      f"{vf},fps={fps_r},setsar=1,format=yuv420p[bpre{k}]")
        # eof_action=repeat: if the b-roll runs out before the window ends, freeze
        # its last frame rather than re-exposing the talking head mid-cutaway
        fc.append(f"{vin}[bpre{k}]overlay=0:0:eof_action=repeat:"
                  f"enable='between(t,{ps_s},{pe_s})'[vb{k}]")
        vin = f"[vb{k}]"

    # captions
    if caption_overlays:
        for k, ov in enumerate(caption_overlays):
            j = cap_base + k
            fc.append(f"{vin}[{j}:v]overlay=(W-w)/2:(H*{_ypct(ir):.4f})-h/2:"
                      f"enable='between(t,{_us_to_s(ov['startUs'])},{_us_to_s(ov['endUs'])})'[vc{k}]")
            vin = f"[vc{k}]"
    elif events and ass_path:
        ass_text = generate_ass(ir, events, cw, ch)
        fc.append(f"{vin}subtitles=filename='{_sub_escape(ass_path)}'[vsub]")
        vin = "[vsub]"

    # logo / watermark (program-anchored corner, top-most)
    if logo:
        lidx = input_index[logo["asset"]]
        tr = logo.get("transform", {})
        logo_w = max(2, min(cw, int(round(tr.get("scale", 0.16) * cw))))  # clamp: never wider than canvas
        opacity = tr.get("opacity", 0.9)
        m = max(0, int(round(logo.get("marginPx", 48) * scale_f)))
        pos = {"top-left": f"{m}:{m}", "top-right": f"W-w-{m}:{m}",
               "bottom-left": f"{m}:H-h-{m}", "bottom-right": f"W-w-{m}:H-h-{m}"}.get(
                   logo.get("corner", "top-right"), f"W-w-{m}:{m}")
        fc.append(f"[{lidx}:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa={opacity}[logo]")
        fc.append(f"{vin}[logo]overlay={pos}:eof_action=repeat[vlogo]")
        vin = "[vlogo]"

    vlast = vin

    # audio: bgm mix (static gain in core; ducking materialized later) + loudness
    alast = "[speech]"
    if bgm is not None:
        bidx = input_index[bgm["asset"]]
        dur_s = _us_to_s(proj.get("durationUs", 0))
        g = _db(bgm.get("gainDb", -18.0))
        fi = bgm.get("fadeInUs", 0) / 1_000_000
        fo = bgm.get("fadeOutUs", 0) / 1_000_000
        fo_st = max(0.0, proj.get("durationUs", 0) / 1_000_000 - (fo or 0))
        fc.append(f"[{bidx}:a]atrim=0:{dur_s},asetpts=PTS-STARTPTS,volume={g},"
                  f"afade=t=in:st=0:d={fi:.3f},afade=t=out:st={fo_st:.3f}:d={fo:.3f}[bgm]")
        fc.append("[speech][bgm]amix=inputs=2:normalize=0:duration=first[amix]")
        alast = "[amix]"
    lufs = out.get("loudnessLufs", -14.0)
    tp = out.get("truePeakDb", -1.0)
    fc.append(f"{alast}loudnorm=I={lufs}:TP={tp}:LRA=11[aout]")

    vcodec = {"h264": "libx264", "h265": "libx265", "hevc": "libx265"}.get(
        out.get("videoCodec", "h264"), "libx264")
    args += ["-filter_complex", ";".join(fc), "-map", vlast, "-map", "[aout]",
             "-c:v", vcodec, "-preset", "ultrafast" if proxy else "medium",
             "-crf", str(out.get("crf", 18) + (6 if proxy else 0)),
             "-pix_fmt", out.get("pixFmt", "yuv420p"), "-r", fps_r,
             "-c:a", "aac", "-b:a", f"{out.get('audioBitrateKbps', 192)}k",
             "-movflags", "+faststart", out_path]
    return {"args": args, "ass": ass_text, "inputs": [assets[a]["path"] for a in used],
            "backend": "imagemagick" if caption_overlays else ("libass" if ass_text else "none")}


def _ypct(ir: dict) -> float:
    cap = E.caption_track_of(ir)
    if cap:
        return cap.get("style", {}).get("position", {}).get("yPct", 0.74)
    return 0.74


# ------------------------------------------------------------------ run
def render(ir: dict, out_path: str, proxy: bool = False) -> dict:
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    E.derive(ir)
    events = E.resolve_caption_events(ir)
    backend = caption_backend()
    scale = 0.5 if proxy else 1.0

    ass_path = None
    caption_overlays = None
    warnings: list[str] = []
    if events:
        if backend == "libass":
            ass_path = os.path.join(out_dir, os.path.basename(out_path) + ".captions.ass")
        elif backend == "imagemagick":
            caption_overlays = render_caption_pngs(ir, events, out_dir, scale)
        else:
            warnings.append("no caption backend (no libass/ImageMagick) - rendering without burned captions")

    built = build_command(ir, out_path, ass_path=ass_path,
                          caption_overlays=caption_overlays, proxy=proxy)
    if built["ass"] and ass_path:
        with open(ass_path, "w", encoding="utf-8") as fh:
            fh.write(built["ass"])

    proc = subprocess.run(built["args"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + proc.stderr[-4000:] + "\n\nCMD:\n" + " ".join(built["args"]))

    return {
        "output": out_path,
        "outputSha256": P.sha256_file(out_path) if os.path.exists(out_path) else None,
        "inputs": {p: P.sha256_file(p) for p in built["inputs"] if os.path.exists(p)},
        "captionBackend": built["backend"],
        "proxy": proxy,
        "durationUs": ir["project"].get("durationUs"),
        "warnings": warnings,
    }
