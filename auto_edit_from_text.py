#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_edit_from_text.py

Renderer otomatis: input teks arahan + input video -> output video final.
Didesain untuk format blok seperti:

00:00–01:20
FORMULA EDITING: ...
AUDIO/BGM: ...
NARASI: "..."

Juga tetap mendukung format lama satu baris:
00:00–00:14 [CUT 3–5s, VO ORISINAL] → Narasi: "..."

Fitur:
- Parser blok multiline FORMULA EDITING / AUDIO-BGM / NARASI.
- Auto micro-cut tiap ±3-5 detik sesuai instruksi.
- Mute audio asli video sumber.
- VO otomatis edge-tts Indonesia Ardi, VO folder manual, atau silent fallback.
- Subtitle burn-in per-kata dengan animasi pop-up + file .srt.
- Efek otomatis dari keyword: ZOOM, GRADE gelap/dingin/hangat/kontras/desaturasi,
  film grain/noise, border, overexposed, glitch ringan, mirror/flip, speed.
- Stock footage/gambar opsional; jika tidak ada, fallback ke source cut/kartu grafis.
- BGM legal/royalty-free opsional dicampur volume rendah.

Catatan: kode ini dibuat untuk workflow recap/komentar transformatif dengan aset legal/berlisensi,
bukan untuk menghindari klaim hak cipta atau sistem deteksi otomatis.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
DASHES = "–—-"
DEFAULT_SIZE = "auto"
FALLBACK_SIZE = "1280x720"
DEFAULT_FPS = 30


@dataclass
class Scene:
    index: int
    start_raw: str
    end_raw: str
    start: float
    end: float
    formula: str
    audio_note: str
    narration: str
    raw_block: str
    kind: str = "source"
    final_duration: float = 0.0
    visual_plan: str = ""
    vo_path: str = ""


@dataclass
class WordCue:
    start: float
    end: float
    text: str


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: Sequence[str], *, quiet: bool = False) -> None:
    if not quiet:
        log("$ " + " ".join(shlex.quote(str(x)) for x in cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Perintah gagal (%s):\n%s\n\nOutput:\n%s" % (
                proc.returncode,
                " ".join(shlex.quote(str(x)) for x in cmd),
                proc.stdout,
            )
        )


def capture(cmd: Sequence[str]) -> str:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Perintah gagal: {' '.join(shlex.quote(str(x)) for x in cmd)}\n{proc.stderr}")
    return proc.stdout.strip()


def is_termux_env() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or Path("/data/data/com.termux/files/usr").exists()


def require_bin(name: str) -> None:
    if shutil.which(name) is None:
        package = "ffmpeg" if name in {"ffmpeg", "ffprobe"} else name
        raise SystemExit(f"ERROR: '{name}' tidak ditemukan. Install di Termux: pkg install {package}")


def auto_install_termux(required_bins: Sequence[str]) -> None:
    missing = [name for name in required_bins if shutil.which(name) is None]
    if not missing or not is_termux_env() or shutil.which("pkg") is None:
        return
    packages: List[str] = []
    if "ffmpeg" in missing or "ffprobe" in missing:
        packages.append("ffmpeg")
    if packages:
        log("[auto-install] Menginstall paket Termux: " + ", ".join(sorted(set(packages))))
        run(["bash", "-lc", "pkg update -y && pkg install -y " + " ".join(sorted(set(packages)))])


def python_module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def ensure_edge_tts(auto_install: bool = True) -> bool:
    if python_module_available("edge_tts"):
        return True
    if not auto_install:
        return False
    log("[auto-install] edge-tts belum ada. Menginstall lewat pip...")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "edge-tts"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        log("WARN: edge-tts gagal dipasang. Output:")
        log(proc.stdout.strip())
        return False
    return python_module_available("edge_tts")


def normalize_text(text: str) -> str:
    return (
        text.replace("\ufeff", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )


def parse_timecode(tc: str) -> float:
    tc = tc.strip().replace(",", ".")
    parts = tc.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Timestamp tidak valid: {tc}")


def fmt_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean_quoted(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    return text.strip().strip('"').strip("'").strip()


def extract_labeled_fields(block: str) -> Dict[str, str]:
    """Ambil label multiline seperti FORMULA EDITING, AUDIO/BGM, NARASI."""
    labels = {
        "FORMULA EDITING": "formula",
        "FORMULA": "formula",
        "EDITING": "formula",
        "AUDIO/BGM": "audio",
        "AUDIO": "audio",
        "BGM": "audio",
        "NARASI": "narration",
        "VOICE OVER": "narration",
        "VO": "narration",
    }
    fields: Dict[str, List[str]] = {"formula": [], "audio": [], "narration": []}
    current: Optional[str] = None
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_ /-]{2,30})\s*:\s*(.*)$", line)
        if m:
            label = re.sub(r"\s+", " ", m.group(1).strip().upper())
            key = labels.get(label)
            if key:
                current = key
                fields[key].append(m.group(2).strip())
                continue
        if current:
            fields[current].append(line)
    return {k: clean_quoted(" ".join(v)) for k, v in fields.items()}


def infer_kind(formula: str, audio: str, narration: str) -> str:
    up = f"{formula} {audio} {narration}".upper()
    if any(x in up for x in ["END CARD", "OUTRO", "LAYAR GELAP", "GRAFIS", "CAPTION", "POSTER"]):
        return "graphic"
    if any(x in up for x in ["STOCK", "CUTAWAY", "VISUAL PENGGANTI"]):
        return "mixed"
    return "source"


def parse_old_one_line(text: str) -> List[Scene]:
    pattern = re.compile(
        rf"^\s*(?P<start>\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:[.,]\d+)?)\s*[{DASHES}]\s*"
        rf"(?P<end>\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:[.,]\d+)?)\s*"
        rf"\[(?P<inst>[^\]]*)\]\s*"
        rf"(?:→|->|=>)\s*(?P<body>.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    scenes: List[Scene] = []
    for m in pattern.finditer(text):
        start_raw, end_raw = m.group("start"), m.group("end")
        body = clean_quoted(m.group("body"))
        nar_match = re.search(r"Narasi\s*:\s*[\"']?(.*?)[\"']?$", body, flags=re.IGNORECASE)
        narration = clean_quoted(nar_match.group(1)) if nar_match else clean_quoted(body)
        start, end = parse_timecode(start_raw), parse_timecode(end_raw)
        if end <= start:
            raise ValueError(f"Timestamp akhir <= awal: {start_raw}-{end_raw}")
        inst = re.sub(r"\s+", " ", m.group("inst").strip())
        scenes.append(Scene(len(scenes) + 1, start_raw, end_raw, start, end, inst, "", narration, m.group(0), infer_kind(inst, "", narration)))
    return scenes


def parse_block_format(text: str) -> List[Scene]:
    time_re = re.compile(
        rf"^\s*(?P<start>\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:[.,]\d+)?)\s*[{DASHES}]\s*"
        rf"(?P<end>\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:[.,]\d+)?)\s*$",
        re.MULTILINE,
    )
    matches = list(time_re.finditer(text))
    scenes: List[Scene] = []
    for i, m in enumerate(matches):
        start_raw, end_raw = m.group("start"), m.group("end")
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[body_start:body_end].strip()
        fields = extract_labeled_fields(block)
        formula = fields.get("formula", "")
        audio = fields.get("audio", "")
        narration = fields.get("narration", "")
        if not narration:
            # Fallback: ambil kalimat terpanjang setelah timestamp jika label tidak ada.
            lines = [x.strip() for x in block.splitlines() if x.strip()]
            narration = clean_quoted(" ".join(lines[-2:])) if lines else ""
        start, end = parse_timecode(start_raw), parse_timecode(end_raw)
        if end <= start:
            raise ValueError(f"Timestamp akhir <= awal: {start_raw}-{end_raw}")
        if not formula:
            formula = "AUTO CUT 3-5s, MUTE AUDIO ASLI, SUBTITLE DINAMIS"
        scenes.append(
            Scene(
                index=len(scenes) + 1,
                start_raw=start_raw,
                end_raw=end_raw,
                start=start,
                end=end,
                formula=formula,
                audio_note=audio,
                narration=narration,
                raw_block=(m.group(0) + "\n" + block).strip(),
                kind=infer_kind(formula, audio, narration),
            )
        )
    return scenes


def parse_script(path: Path) -> List[Scene]:
    text = normalize_text(path.read_text(encoding="utf-8", errors="replace"))
    block_scenes = parse_block_format(text)
    if block_scenes:
        return block_scenes
    old_scenes = parse_old_one_line(text)
    if old_scenes:
        return old_scenes
    raise ValueError(
        "Tidak ada segmen terbaca. Format yang didukung:\n"
        "00:00–01:20\nFORMULA EDITING: ...\nAUDIO/BGM: ...\nNARASI: \"...\"\n\n"
        "atau format satu baris: 00:00–00:14 [CUT 3–5s] → Narasi: \"...\""
    )


def ffprobe_duration(path: Path) -> float:
    out = capture([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(out)
    except ValueError:
        return 0.0




def parse_size_value(size: str) -> Tuple[int, int]:
    """Parse string ukuran WxH dan pastikan nilainya aman untuk libx264."""
    raw = (size or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*x\s*(\d+)", raw)
    if not m:
        raise ValueError(f"Resolusi tidak valid: {size!r}. Pakai 'auto' atau format seperti 1280x720.")
    w, h = int(m.group(1)), int(m.group(2))
    if w < 16 or h < 16:
        raise ValueError(f"Resolusi terlalu kecil: {size!r}")
    # Codec H.264/yuv420p paling aman memakai angka genap.
    w -= w % 2
    h -= h % 2
    return max(16, w), max(16, h)


def normalize_size_value(size: str) -> str:
    w, h = parse_size_value(size)
    return f"{w}x{h}"


def ffprobe_video_size(path: Path) -> Optional[Tuple[int, int]]:
    """Ambil resolusi video sumber. Metadata rotasi 90/270 ikut diperhitungkan."""
    try:
        out = capture([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
            "-of", "json", str(path),
        ])
        data = json.loads(out or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        st = streams[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        if w <= 0 or h <= 0:
            return None

        rotation = 0
        rotate_tag = (st.get("tags") or {}).get("rotate")
        if rotate_tag not in (None, ""):
            try:
                rotation = int(float(str(rotate_tag)))
            except ValueError:
                rotation = 0
        for item in st.get("side_data_list") or []:
            if "rotation" in item:
                try:
                    rotation = int(float(str(item["rotation"])))
                except ValueError:
                    pass
        if abs(rotation) % 180 == 90:
            w, h = h, w

        w -= w % 2
        h -= h % 2
        return max(16, w), max(16, h)
    except Exception as exc:
        log(f"WARN: gagal membaca resolusi video sumber: {exc}")
        return None


def resolve_output_size(requested_size: str, source: Optional[Path]) -> str:
    """Resolusi final. Default auto = sama dengan resolusi/rasio video sumber."""
    raw = (requested_size or "auto").strip().lower()
    if raw in {"auto", "otomatis", "source", "original", "asli"}:
        if source:
            detected = ffprobe_video_size(source)
            if detected:
                w, h = detected
                size = f"{w}x{h}"
                log(f"[size] auto mengikuti video sumber: {size}")
                return size
        size = normalize_size_value(FALLBACK_SIZE)
        log(f"[size] auto tidak bisa membaca video sumber, fallback: {size}")
        return size
    size = normalize_size_value(raw)
    log(f"[size] manual: {size}")
    return size

def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name.strip("._-")[:max_len] or "file"


def find_font(user_font: Optional[Path] = None) -> str:
    candidates: List[Path] = []
    if user_font:
        candidates.append(user_font)
    candidates.extend([
        Path("/system/fonts/Roboto-Regular.ttf"),
        Path("/system/fonts/NotoSans-Regular.ttf"),
        Path("/system/fonts/DroidSans.ttf"),
        Path("/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ])
    for p in candidates:
        if p.exists():
            return str(p)
    return "Arial"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def wrap_ass(text: str, width: int = 46, max_lines: int = 3) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    lines = textwrap.wrap(text, width=width, break_long_words=False, replace_whitespace=False)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return "\\N".join(ass_escape(x) for x in lines)


def ff_filter_path(path: Path) -> str:
    # Untuk filtergraph ffmpeg: ass='path'. Escape backslash dan apostrof.
    s = str(path.resolve()).replace("\\", "\\\\").replace("'", r"\'")
    return s


def subtitle_font_size(size: str) -> int:
    _w, h = parse_size_value(size)
    return max(20, min(34, round(h * 0.030)))


def subtitle_margin_v(size: str) -> int:
    _w, h = parse_size_value(size)
    # Margin dari bawah. Nilai ini membuat teks berada di tengah-bawah,
    # tidak menempel ke tepi bawah frame.
    return max(64, min(190, round(h * 0.13)))


def subtitle_outline(size: str) -> int:
    _w, h = parse_size_value(size)
    return max(1, min(3, round(h * 0.0024)))


def clean_word_token(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip())
    text = text.strip('"“”‘’`')
    return text


def split_words(text: str) -> List[str]:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if not clean:
        return []
    # Ambil token kata beserta tanda baca yang menempel. Ini menjaga subtitle tetap natural.
    words = [clean_word_token(x) for x in re.findall(r"\S+", clean)]
    return [x for x in words if x]


def edge_time_to_seconds(value: object) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    # edge-tts umumnya memberi offset/duration dalam satuan 100 ns.
    if abs(v) > 10000:
        return v / 10_000_000.0
    return v


def fallback_word_cues(text: str, duration: float) -> List[WordCue]:
    words = split_words(text)
    if not words:
        return []
    duration = max(0.20, float(duration or 0.20))
    gap = 0.018 if len(words) > 1 else 0.0
    available = max(0.12, duration - gap * (len(words) - 1))
    weights = [max(1.0, min(12.0, len(re.sub(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ]+", "", w)) or len(w))) for w in words]
    total_weight = max(1.0, sum(weights))
    cues: List[WordCue] = []
    t = 0.0
    for i, (word, weight) in enumerate(zip(words, weights)):
        dur = available * weight / total_weight
        if i == len(words) - 1:
            end = duration
        else:
            end = min(duration, t + dur)
        if end <= t:
            end = min(duration, t + 0.05)
        cues.append(WordCue(max(0.0, t), max(t + 0.04, end), word))
        t = end + gap
        if t >= duration:
            break
    return cues


def load_edge_word_cues(words_json: Optional[Path], text: str, duration: float) -> List[WordCue]:
    if not words_json or not words_json.exists():
        return fallback_word_cues(text, duration)
    try:
        data = json.loads(words_json.read_text(encoding="utf-8"))
    except Exception:
        return fallback_word_cues(text, duration)

    raw: List[Tuple[float, float, str]] = []
    for item in data if isinstance(data, list) else []:
        word = clean_word_token(item.get("text", "") if isinstance(item, dict) else "")
        if not word:
            continue
        start = edge_time_to_seconds(item.get("offset", 0))
        dur = edge_time_to_seconds(item.get("duration", 0))
        raw.append((max(0.0, start), max(0.03, dur), word))
    raw.sort(key=lambda x: x[0])
    if not raw:
        return fallback_word_cues(text, duration)

    cues: List[WordCue] = []
    total = max(0.20, float(duration or 0.20))
    for i, (start, dur, word) in enumerate(raw):
        if start >= total:
            continue
        next_start = raw[i + 1][0] if i + 1 < len(raw) else total
        natural_end = start + max(0.16, min(0.80, dur + 0.08))
        end = min(total, natural_end)
        if next_start > start:
            end = min(end, max(start + 0.04, next_start - 0.01))
        if end <= start:
            end = min(total, start + 0.06)
        cues.append(WordCue(start, end, word))
    return cues or fallback_word_cues(text, duration)


def write_ass(path: Path, text: str, duration: float, *, font: str, mode: str = "subtitle", size: str = FALLBACK_SIZE, words_json: Optional[Path] = None) -> None:
    font_name = Path(font).stem if str(font).lower().endswith(('.ttf', '.otf')) else str(font)
    play_w, play_h = parse_size_value(size)

    if mode == "subtitle":
        fontsize = subtitle_font_size(size)
        margin_v = subtitle_margin_v(size)
        outline = subtitle_outline(size)
        cues = load_edge_word_cues(words_json, text, duration)
        lines = []
        pop_tag = r"{\an2\fscx55\fscy55\t(0,100,\fscx118\fscy118)\t(100,190,\fscx100\fscy100)\fad(20,70)}"
        for cue in cues:
            if cue.end <= cue.start:
                continue
            word = ass_escape(cue.text)
            lines.append(f"Dialogue: 0,{fmt_ass_time(cue.start)},{fmt_ass_time(cue.end)},Pop,,0,0,0,,{pop_tag}{word}")
        if not lines:
            lines.append(f"Dialogue: 0,{fmt_ass_time(0)},{fmt_ass_time(duration)},Pop,,0,0,0,,{ass_escape(text)}")
        events = "\n".join(lines)
        content = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {play_w}
PlayResY: {play_h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,{font_name},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,{outline},1,2,70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events}
"""
        path.write_text(content, encoding="utf-8")
        return

    if mode == "card":
        fontsize, alignment, margin_v, outline, back = 42, 5, 42, 2, "&H80000000"
        wrapped = wrap_ass(text, width=34, max_lines=7)
    elif mode == "intro":
        fontsize, alignment, margin_v, outline, back = 50, 5, 42, 3, "&H80000000"
        wrapped = wrap_ass(text, width=30, max_lines=5)
    else:
        fontsize, alignment, margin_v, outline, back = 34, 2, 46, 2, "&H99000000"
        wrapped = wrap_ass(text, width=48, max_lines=3)
    content = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {play_w}
PlayResY: {play_h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,{back},1,0,0,0,100,100,0,0,3,{outline},0,{alignment},70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{fmt_ass_time(0)},{fmt_ass_time(duration)},Default,,0,0,0,,{wrapped}
"""
    path.write_text(content, encoding="utf-8")


def write_global_srt(path: Path, scenes: Sequence[Scene], intro_duration: float, outro_duration: float) -> None:
    t = intro_duration
    lines: List[str] = []
    for i, scene in enumerate(scenes, start=1):
        start, end = t, t + scene.final_duration
        lines.extend([str(i), f"{fmt_srt_time(start)} --> {fmt_srt_time(end)}", scene.narration, ""])
        t = end
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_micro_seconds(text: str, default: float = 4.5) -> float:
    up = text.upper()
    # "tiap ±4–5 detik", "per ±5 detik", "cut 2–3 detik"
    patterns = [
        r"(?:TIAP|PER|CUT|POTONGAN|POLA)[^0-9]{0,16}(\d+(?:[.,]\d+)?)\s*[–—\-]\s*(\d+(?:[.,]\d+)?)\s*(?:DETIK|S|SEC)",
        r"(?:TIAP|PER|CUT|POTONGAN|POLA)[^0-9]{0,16}(\d+(?:[.,]\d+)?)\s*(?:DETIK|S|SEC)",
        r"(\d+(?:[.,]\d+)?)\s*[–—\-]\s*(\d+(?:[.,]\d+)?)\s*(?:DETIK|S|SEC)",
    ]
    for pat in patterns:
        m = re.search(pat, up, flags=re.IGNORECASE)
        if not m:
            continue
        if len(m.groups()) >= 2 and m.group(2):
            a = float(m.group(1).replace(",", "."))
            b = float(m.group(2).replace(",", "."))
            return max(1.0, min(8.0, (a + b) / 2.0))
        a = float(m.group(1).replace(",", "."))
        return max(1.0, min(8.0, a))
    if "CUT SINGKAT" in up or "CUT CEPAT" in up:
        return 2.5
    return default


def parse_zoom(text: str, fallback: float = 1.08) -> float:
    m = re.search(r"ZOOM\s*(\d{2,3})(?:\s*%)?", text, flags=re.IGNORECASE)
    if m:
        val = float(m.group(1)) / 100.0
        return max(1.0, min(1.35, val))
    return fallback if "ZOOM" in text.upper() else 1.0


def parse_speed(text: str) -> float:
    m = re.search(r"SPEED\s*(?:X|=|:)?\s*(\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    if m:
        return max(0.25, min(4.0, float(m.group(1).replace(",", "."))))
    up = text.upper()
    if any(x in up for x in ["SPEED UP", "CEPAT", "FAST"]):
        return 1.15
    if any(x in up for x in ["SLOW", "SLOWMO", "PELAn".upper()]):
        return 0.82
    return 1.0


def effect_filter(instruction: str, *, size: str, fps: int, variant: int = 0) -> str:
    w, h = parse_size_value(size)
    inst = instruction.upper()
    filters: List[str] = [
        f"scale={w}:{h}:force_original_aspect_ratio=increase",
        f"crop={w}:{h}",
        f"fps={fps}",
        "setsar=1",
        "format=yuv420p",
    ]

    zoom = parse_zoom(instruction)
    # Variasi zoom halus walaupun instruksi sama, supaya tidak terasa loop statis.
    if zoom > 1.0 or variant % 3 == 1:
        z = zoom if zoom > 1.0 else 1.045
        filters.append(f"scale=trunc(iw*{z:.4f}/2)*2:trunc(ih*{z:.4f}/2)*2")
        filters.append(f"crop={w}:{h}")

    speed = parse_speed(instruction)
    if speed != 1.0:
        filters.append(f"setpts={1/speed:.6f}*PTS")
    else:
        filters.append("setpts=PTS-STARTPTS")

    if "MIRROR" in inst or "FLIP" in inst:
        filters.append("hflip")

    if any(x in inst for x in ["OVEREXPOSED", "OVEREXPOSE", "TERANG"]):
        filters.append("eq=contrast=1.08:saturation=0.95:brightness=0.08")
    elif any(x in inst for x in ["DESATURASI", "DESATURATION", "PUCAT"]):
        filters.append("eq=contrast=1.08:saturation=0.55:brightness=-0.01")
    elif any(x in inst for x in ["GRADE GELAP", "DARK", "GELAP"]):
        filters.append("eq=contrast=1.14:saturation=0.86:brightness=-0.06")
    elif any(x in inst for x in ["GRADE DINGIN", "GRADE BIRU", "BIRU", "DINGIN"]):
        filters.append("eq=contrast=1.10:saturation=0.82:brightness=-0.03")
    elif any(x in inst for x in ["HANGAT", "WARM"]):
        filters.append("eq=contrast=1.06:saturation=1.08:brightness=0.02")
    elif any(x in inst for x in ["MERAH", "RED"]):
        filters.append("eq=contrast=1.14:saturation=1.18:brightness=-0.02")
    elif any(x in inst for x in ["KONTRAS", "CONTRAST"]):
        filters.append("eq=contrast=1.22:saturation=1.02:brightness=-0.02")
    elif any(x in inst for x in ["TEAL", "SINEMATIK", "CINEMATIC"]):
        filters.append("eq=contrast=1.10:saturation=0.90:brightness=-0.02")

    if any(x in inst for x in ["FILM GRAIN", "GRAIN", "NOISE", "KASAR"]):
        filters.append("noise=alls=7:allf=t")

    if any(x in inst for x in ["BORDER", "BINGKAI"]):
        filters.append("drawbox=x=18:y=18:w=iw-36:h=ih-36:color=white@0.50:t=3")

    if any(x in inst for x in ["GLITCH", "DATA ERROR"]):
        # Efek ringan yang masih cukup aman untuk HP.
        filters.append("noise=alls=10:allf=t")

    return ",".join(filters)


def list_media(folder: Optional[Path], exts: set[str]) -> List[Path]:
    if not folder or not folder.exists() or not folder.is_dir():
        return []
    return [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]


def keyword_score(path: Path, keywords: str) -> int:
    name = re.sub(r"[_\-.]+", " ", path.stem.lower())
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", keywords) if len(w) >= 4]
    return sum(1 for w in words if w in name)


def choose_stock(stock_dir: Optional[Path], keywords: str, rng: random.Random) -> Optional[Path]:
    files = list_media(stock_dir, VIDEO_EXTS | IMAGE_EXTS)
    if not files:
        return None
    scored = [(keyword_score(p, keywords), rng.random(), p) for p in files]
    scored.sort(reverse=True)
    return scored[0][2]


def choose_bgm(bgm: Optional[Path]) -> Optional[Path]:
    if not bgm:
        return None
    if bgm.is_file() and bgm.suffix.lower() in AUDIO_EXTS:
        return bgm
    files = list_media(bgm, AUDIO_EXTS)
    return files[0] if files else None


def make_silence(path: Path, duration: float) -> None:
    run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(path),
    ], quiet=True)


def make_vo_edge(text: str, out_wav: Path, *, voice: str, rate: str, volume: str, pitch: str, auto_install: bool) -> bool:
    clean = re.sub(r"\s+", " ", text.strip())
    if not clean:
        return False
    if not ensure_edge_tts(auto_install=auto_install):
        log("WARN: edge-tts belum tersedia. VO segmen ini diganti silent.")
        return False
    tmp_mp3 = out_wav.with_suffix(".edge.mp3")
    words_json = out_wav.with_suffix(".words.json")
    tmp_mp3.unlink(missing_ok=True)
    words_json.unlink(missing_ok=True)
    out_wav.unlink(missing_ok=True)
    py = r'''
import asyncio
import json
import sys
import edge_tts

async def main():
    text, out_mp3, out_words, voice, rate, volume, pitch = sys.argv[1:8]
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume, pitch=pitch)
    words = []
    with open(out_mp3, "wb") as audio_file:
        async for chunk in communicate.stream():
            typ = chunk.get("type")
            if typ == "audio":
                audio_file.write(chunk.get("data", b""))
            elif typ == "WordBoundary":
                words.append({
                    "offset": chunk.get("offset", 0),
                    "duration": chunk.get("duration", 0),
                    "text": chunk.get("text", ""),
                })
    with open(out_words, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)

asyncio.run(main())
'''
    proc = subprocess.run(
        [sys.executable, "-c", py, clean, str(tmp_mp3), str(words_json), voice, rate, volume, pitch],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0 or not tmp_mp3.exists() or tmp_mp3.stat().st_size < 1000:
        log("WARN: edge-tts gagal membuat VO. Output:")
        log(proc.stdout.strip())
        return False
    proc2 = subprocess.run([
        "ffmpeg", "-y", "-i", str(tmp_mp3), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out_wav),
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc2.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 1000:
        log("WARN: Konversi VO gagal. Output:")
        log(proc2.stdout.strip())
        return False
    return True


def existing_vo(vo_dir: Optional[Path], index: int) -> Optional[Path]:
    if not vo_dir or not vo_dir.exists():
        return None
    names = [f"{index:03d}", f"seg_{index:03d}", f"scene_{index:03d}", f"segment_{index:03d}"]
    for stem in names:
        for ext in AUDIO_EXTS:
            p = vo_dir / f"{stem}{ext}"
            if p.exists():
                return p
    return None


def prepare_vo(scene: Scene, work_dir: Path, args: argparse.Namespace) -> Path:
    out_wav = work_dir / f"scene_{scene.index:03d}_vo.wav"
    vo_dir = Path(args.vo_dir).expanduser() if args.vo_dir else None
    found = existing_vo(vo_dir, scene.index)
    if found:
        run([
            "ffmpeg", "-y", "-i", str(found), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(out_wav),
        ], quiet=True)
        scene.vo_path = str(found)
        return out_wav

    ok = False
    if args.tts == "edge":
        ok = make_vo_edge(
            scene.narration,
            out_wav,
            voice=args.edge_voice,
            rate=args.edge_rate,
            volume=args.edge_volume,
            pitch=args.edge_pitch,
            auto_install=not args.no_auto_install,
        )
    if not ok:
        # Estimasi natural bahasa Indonesia: 2.5-2.9 kata/detik, beri minimum.
        estimated = max(args.min_scene_duration, len(scene.narration.split()) / 2.65)
        make_silence(out_wav, estimated)
        scene.vo_path = "silent_fallback"
    else:
        scene.vo_path = "edge_tts"
    return out_wav


def render_card_visual(out_path: Path, duration: float, ass_path: Optional[Path] = None, *, size: str, fps: int, card_type: str = "card") -> None:
    color = "#0B1020" if card_type != "outro" else "#101010"
    vf_parts = ["noise=alls=3:allf=t"]
    if ass_path:
        vf_parts.append(f"ass='{ff_filter_path(ass_path)}'")
    vf_parts.append("format=yuv420p")
    vf = ",".join(vf_parts)
    run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d={duration:.3f}",
        "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", str(out_path),
    ], quiet=True)


def overlay_ass_on_video(video_in: Path, out_path: Path, ass_path: Path) -> None:
    vf = f"ass='{ff_filter_path(ass_path)}',format=yuv420p"
    run([
        "ffmpeg", "-y", "-i", str(video_in), "-map", "0:v:0", "-vf", vf,
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", str(out_path),
    ], quiet=True)


def render_source_chunk(source: Path, out_path: Path, start: float, duration: float, instruction: str, *, size: str, fps: int, variant: int) -> None:
    vf = effect_filter(instruction, size=size, fps=fps, variant=variant)
    run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out_path),
    ], quiet=True)


def render_stock_chunk(stock: Path, out_path: Path, duration: float, instruction: str, *, size: str, fps: int, variant: int) -> None:
    vf = effect_filter(instruction + " ZOOM", size=size, fps=fps, variant=variant)
    if stock.suffix.lower() in IMAGE_EXTS:
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(stock), "-t", f"{duration:.3f}",
            "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(stock), "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out_path),
        ]
    run(cmd, quiet=True)


def concat_video_only(paths: Sequence[Path], out_path: Path, list_path: Path) -> None:
    def line(p: Path) -> str:
        s = str(p.resolve()).replace("'", "'\\''")
        return f"file '{s}'"
    list_path.write_text("\n".join(line(p) for p in paths) + "\n", encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(out_path)], quiet=True)


def mux_video_audio(video: Path, audio: Path, out_path: Path, duration: float) -> None:
    run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(audio), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-ar", "44100", "-ac", "2", "-shortest", str(out_path),
    ], quiet=True)


def should_use_stock(scene: Scene, chunk_index: int) -> bool:
    up = scene.formula.upper()
    if "STOCK" not in up and "CUTAWAY" not in up and "VISUAL PENGGANTI" not in up:
        return False
    # Selang-seling: source -> stock -> source, kecuali instruksi memang stock penuh.
    if "GUNAKAN" in up and ("STOCK" in up or "VISUAL PENGGANTI" in up) and "SUMBER" not in up:
        return True
    return chunk_index % 3 == 1


def render_scene_visual(scene: Scene, source: Optional[Path], stock_dir: Optional[Path], work_dir: Path, ass_path: Path, args: argparse.Namespace, rng: random.Random) -> Path:
    out_scene_video = work_dir / f"scene_{scene.index:03d}_visual.mp4"
    raw_scene_video = work_dir / f"scene_{scene.index:03d}_visual_raw.mp4"
    span = max(0.5, scene.end - scene.start)
    micro = parse_micro_seconds(scene.formula, default=args.micro_seconds)
    n = max(1, math.ceil(scene.final_duration / micro))
    chunk_durations: List[float] = []
    remaining = scene.final_duration
    for i in range(n):
        d = min(micro, remaining)
        if i == n - 1:
            d = remaining
        d = max(0.20, d)
        chunk_durations.append(d)
        remaining -= d
        if remaining <= 0.05:
            break

    selected_stock = choose_stock(stock_dir, scene.formula + " " + scene.narration, rng)

    # Mode fallback tanpa source/stock: latar bersih + subtitle per-kata.
    if not source and not selected_stock:
        render_card_visual(raw_scene_video, scene.final_duration, None, size=args.size, fps=args.fps)
        overlay_ass_on_video(raw_scene_video, out_scene_video, ass_path)
        scene.visual_plan = "text_card_no_source"
        return out_scene_video

    chunks: List[Path] = []
    visual_notes: List[str] = []
    for i, dur in enumerate(chunk_durations):
        chunk_out = work_dir / f"scene_{scene.index:03d}_chunk_{i+1:02d}.mp4"
        use_stock = selected_stock is not None and (should_use_stock(scene, i) or not source or scene.kind == "graphic")
        if use_stock and selected_stock:
            render_stock_chunk(selected_stock, chunk_out, dur, scene.formula, size=args.size, fps=args.fps, variant=i)
            visual_notes.append(f"stock:{selected_stock.name}")
        elif source:
            if len(chunk_durations) == 1:
                start = scene.start
            else:
                max_start = max(scene.start, scene.end - max(0.5, dur))
                frac = i / max(1, len(chunk_durations) - 1)
                start = scene.start + (max_start - scene.start) * frac
            start = max(scene.start, min(start, max(scene.start, scene.end - 0.3)))
            # Jika output durasi lebih lama dari sisa rentang sumber, tetap ambil dari start; ffmpeg akan membatasi.
            render_source_chunk(source, chunk_out, start, dur, scene.formula, size=args.size, fps=args.fps, variant=i)
            visual_notes.append(f"source@{start:.2f}s")
        else:
            render_card_visual(chunk_out, dur, None, size=args.size, fps=args.fps)
            visual_notes.append("text_card")
        chunks.append(chunk_out)

    if len(chunks) == 1:
        shutil.copy2(chunks[0], raw_scene_video)
    else:
        concat_video_only(chunks, raw_scene_video, work_dir / f"scene_{scene.index:03d}_chunks.txt")
    overlay_ass_on_video(raw_scene_video, out_scene_video, ass_path)
    scene.visual_plan = ", ".join(visual_notes[:8]) + (" ..." if len(visual_notes) > 8 else "")
    return out_scene_video


def make_intro_outro(text: str, out_path: Path, work_dir: Path, *, duration: float, font: str, size: str, fps: int, card_type: str) -> None:
    ass = work_dir / f"{card_type}.ass"
    wav = work_dir / f"{card_type}.wav"
    vid = work_dir / f"{card_type}_video.mp4"
    write_ass(ass, text, duration, font=font, mode="intro", size=size)
    make_silence(wav, duration)
    render_card_visual(vid, duration, ass, size=size, fps=fps, card_type=card_type)
    mux_video_audio(vid, wav, out_path, duration)


def mix_bgm(video_in: Path, bgm: Path, out_path: Path, volume: float) -> None:
    run([
        "ffmpeg", "-y", "-i", str(video_in), "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex",
        f"[0:a]volume=1.0[a0];[1:a]volume={volume:.3f}[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(out_path),
    ])


def preview_scenes(scenes: Sequence[Scene]) -> None:
    print(json.dumps([asdict(x) for x in scenes], ensure_ascii=False, indent=2))


def render_project(args: argparse.Namespace) -> None:
    script = Path(args.script).expanduser().resolve()
    if not script.exists():
        raise SystemExit(f"ERROR: file script tidak ditemukan: {script}")
    source = Path(args.source).expanduser().resolve() if args.source else None
    stock_dir = Path(args.stock_dir).expanduser().resolve() if args.stock_dir else None
    bgm_path = Path(args.bgm).expanduser().resolve() if args.bgm else None
    out_path = Path(args.out).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve() if args.work_dir else out_path.parent / "auto_edit_work"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    scenes = parse_script(script)
    if args.preview:
        preview_scenes(scenes)
        return

    if not args.no_auto_install:
        auto_install_termux(["ffmpeg", "ffprobe"])
    require_bin("ffmpeg")
    require_bin("ffprobe")

    if source and not source.exists():
        raise SystemExit(f"ERROR: video sumber tidak ditemukan: {source}")
    if not source:
        log("WARN: source video kosong. Renderer memakai stock/card fallback.")
    if stock_dir and not stock_dir.exists():
        log(f"WARN: stock-dir tidak ditemukan: {stock_dir}. Fallback ke source/card.")
    if bgm_path and not bgm_path.exists():
        log(f"WARN: BGM tidak ditemukan: {bgm_path}. Output tanpa BGM.")
        bgm_path = None

    args.size = resolve_output_size(args.size, source)

    if args.tts == "edge":
        ensure_edge_tts(auto_install=not args.no_auto_install)

    font = find_font(Path(args.font).expanduser() if args.font else None)
    rng = random.Random(args.seed)
    segment_files: List[Path] = []

    intro_duration = 0.0
    if args.with_intro and not args.no_intro:
        intro_duration = max(1.0, args.intro_duration)
        intro_path = work_dir / "000_intro.mp4"
        log("[intro] membuat intro")
        make_intro_outro(args.intro_text, intro_path, work_dir, duration=intro_duration, font=font, size=args.size, fps=args.fps, card_type="intro")
        segment_files.append(intro_path)

    for scene in scenes:
        log(f"[{scene.index:03d}/{len(scenes):03d}] {scene.start_raw}-{scene.end_raw} | render otomatis")
        vo_wav = prepare_vo(scene, work_dir, args)
        vo_dur = max(0.20, ffprobe_duration(vo_wav))
        scene.final_duration = max(args.min_scene_duration, vo_dur + args.audio_tail)
        if args.max_scene_duration > 0:
            scene.final_duration = min(scene.final_duration, args.max_scene_duration)

        ass = work_dir / f"scene_{scene.index:03d}.ass"
        write_ass(ass, scene.narration, scene.final_duration, font=font, mode="subtitle", size=args.size, words_json=vo_wav.with_suffix(".words.json"))
        visual = render_scene_visual(scene, source, stock_dir, work_dir, ass, args, rng)
        out_seg = work_dir / f"scene_{scene.index:03d}.mp4"
        mux_video_audio(visual, vo_wav, out_seg, scene.final_duration)
        segment_files.append(out_seg)

    outro_duration = 0.0
    if args.with_outro and not args.no_outro:
        outro_duration = max(1.0, args.outro_duration)
        outro_path = work_dir / f"{len(scenes)+1:03d}_outro.mp4"
        log("[outro] membuat outro")
        make_intro_outro(args.outro_text, outro_path, work_dir, duration=outro_duration, font=font, size=args.size, fps=args.fps, card_type="outro")
        segment_files.append(outro_path)

    no_bgm = work_dir / "final_no_bgm.mp4"
    log("[concat] menyatukan semua scene")
    concat_video_only(segment_files, no_bgm, work_dir / "concat_final.txt")

    bgm_file = choose_bgm(bgm_path)
    if bgm_file:
        log(f"[bgm] mencampur BGM: {bgm_file}")
        mix_bgm(no_bgm, bgm_file, out_path, args.bgm_volume)
    else:
        log("[bgm] tidak ada BGM, output memakai VO/silent saja")
        shutil.copy2(no_bgm, out_path)

    srt_path = out_path.with_suffix(".srt")
    meta_path = out_path.with_suffix(".segments.json")
    write_global_srt(srt_path, scenes, intro_duration, outro_duration)
    meta_path.write_text(json.dumps([asdict(x) for x in scenes], ensure_ascii=False, indent=2), encoding="utf-8")

    log("\nSELESAI")
    log(f"Video final : {out_path}")
    log(f"Subtitle SRT: {srt_path}")
    log(f"Metadata    : {meta_path}")
    log(f"Work dir    : {work_dir}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Auto editor video dari teks arahan + video sumber.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--script", required=True, help="File teks arahan/script.")
    p.add_argument("--source", default="", help="Video sumber legal/berlisensi. Kosongkan untuk stock/card fallback.")
    p.add_argument("--stock-dir", default="stock", help="Folder stock footage/gambar legal opsional.")
    p.add_argument("--bgm", default="", help="File/folder BGM legal opsional.")
    p.add_argument("--vo-dir", default="", help="Folder VO siap pakai, nama 001.wav / scene_001.wav, dst.")
    p.add_argument("--out", default="output/final_auto_edit.mp4", help="Path output final MP4.")
    p.add_argument("--work-dir", default="", help="Folder kerja sementara.")
    p.add_argument("--size", default=DEFAULT_SIZE, help="Resolusi output. Default auto = mengikuti resolusi/rasio video sumber. Bisa manual: 1280x720 / 1080x1920 / 720x960.")
    p.add_argument("--fps", type=int, default=DEFAULT_FPS, help="FPS output.")
    p.add_argument("--font", default="", help="Path font .ttf/.otf opsional.")
    p.add_argument("--micro-seconds", type=float, default=4.5, help="Durasi micro-cut default jika tidak disebut di teks.")
    p.add_argument("--min-scene-duration", type=float, default=3.0, help="Durasi minimum scene final.")
    p.add_argument("--max-scene-duration", type=float, default=0.0, help="Batas maksimum scene final. 0 = tidak dibatasi.")
    p.add_argument("--audio-tail", type=float, default=0.25, help="Jeda kecil setelah VO per scene.")
    p.add_argument("--tts", choices=["edge", "none"], default="edge", help="edge = TTS otomatis online; none = VO folder/silent.")
    p.add_argument("--edge-voice", default="id-ID-ArdiNeural", help="Voice edge-tts. Default Indonesia Ardi.")
    p.add_argument("--edge-rate", default="+0%", help="Rate edge-tts, contoh -10%, +0%, +15%.")
    p.add_argument("--edge-volume", default="+0%", help="Volume edge-tts.")
    p.add_argument("--edge-pitch", default="+0Hz", help="Pitch edge-tts.")
    p.add_argument("--bgm-volume", type=float, default=0.13, help="Volume BGM saat dicampur.")
    p.add_argument("--intro-text", default="RECAP SPOILER\nNarasi orisinal + materi legal/berlisensi", help="Teks intro.")
    p.add_argument("--outro-text", default="Terima kasih sudah menonton.\nSubscribe untuk recap berikutnya.", help="Teks outro.")
    p.add_argument("--intro-duration", type=float, default=2.5, help="Durasi intro.")
    p.add_argument("--outro-duration", type=float, default=3.0, help="Durasi outro.")
    p.add_argument("--no-intro", action="store_true", help="Matikan intro. Intro memang default mati.")
    p.add_argument("--no-outro", action="store_true", help="Matikan outro. Outro memang default mati.")
    p.add_argument("--with-intro", action="store_true", help="Aktifkan lagi intro teks di awal jika memang dibutuhkan.")
    p.add_argument("--with-outro", action="store_true", help="Aktifkan lagi outro teks di akhir jika memang dibutuhkan.")
    p.add_argument("--seed", type=int, default=20260608, help="Seed variasi stock/cut.")
    p.add_argument("--preview", action="store_true", help="Hanya parse script dan tampilkan JSON, tidak render.")
    p.add_argument("--no-auto-install", action="store_true", help="Matikan auto install dependency Termux.")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        render_project(args)
    except KeyboardInterrupt:
        raise SystemExit("\nDibatalkan.")
    except Exception as exc:
        raise SystemExit(f"\nERROR: {exc}")


if __name__ == "__main__":
    main()
