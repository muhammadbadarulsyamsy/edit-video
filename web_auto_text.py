#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web lokal sederhana untuk auto_edit_from_text.py.
Jalankan: python web_auto_text.py --host 127.0.0.1 --port 7860 --open
"""

from __future__ import annotations

import argparse
import html
import json
import importlib.util
import mimetypes
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.parse import parse_qs, quote, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
RENDERER = APP_DIR / "auto_edit_from_text.py"
PROJECTS_DIR = APP_DIR / "projects"
UPLOADS_DIR = APP_DIR / "uploads"
DEFAULT_PORT = 7860
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TEXT_EXTS = {".txt"}
PROJECTS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)


@dataclass
class Job:
    id: str
    status: str = "running"
    logs: List[str] = field(default_factory=list)
    output_path: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    returncode: Optional[int] = None

    def add(self, text: str) -> None:
        text = text.rstrip("\n")
        if not text:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {text}")
        if len(self.logs) > 3000:
            self.logs = self.logs[-3000:]


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def which(name: str) -> str:
    return shutil.which(name) or ""


def is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or Path("/data/data/com.termux/files/usr").exists()


def module_ok(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def json_response(h: BaseHTTPRequestHandler, data: object, status: int = 200) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(raw)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(raw)


def text_response(h: BaseHTTPRequestHandler, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
    raw = text.encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(raw)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(raw)


def read_body(h: BaseHTTPRequestHandler) -> bytes:
    n = int(h.headers.get("Content-Length", "0") or "0")
    return h.rfile.read(n) if n > 0 else b""


def read_json(h: BaseHTTPRequestHandler) -> dict:
    raw = read_body(h)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def read_multipart(h: BaseHTTPRequestHandler) -> tuple[dict, dict]:
    ctype = h.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        raise ValueError("Request harus multipart/form-data.")
    raw = read_body(h)
    header = (f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8")
    msg = BytesParser(policy=email_default_policy).parsebytes(header + raw)
    fields: dict = {}
    files: dict = {}
    if not msg.is_multipart():
        raise ValueError("Upload multipart tidak valid.")
    for part in msg.iter_parts():
        disp = part.get("Content-Disposition", "")
        if "form-data" not in disp:
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename:
            if not data:
                continue
            files.setdefault(name, []).append({"filename": filename, "data": data})
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = data.decode(charset, errors="replace")
    return fields, files


def status_data() -> dict:
    return {
        "app_dir": str(APP_DIR),
        "renderer_exists": RENDERER.exists(),
        "is_termux": is_termux(),
        "python": sys.executable,
        "ffmpeg": which("ffmpeg"),
        "ffprobe": which("ffprobe"),
        "edge_tts": module_ok("edge_tts"),
        "edge_voice_default": "id-ID-ArdiNeural",
        "ready_basic": RENDERER.exists() and bool(which("ffmpeg")) and bool(which("ffprobe")),
        "ready_tts": module_ok("edge_tts"),
        "projects_dir": str(PROJECTS_DIR),
    }


def safe_name(text: str) -> str:
    text = Path(text or "file").name
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return text or "file"


def unique_path(folder: Path, filename: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem or "file"
    suffix = Path(filename).suffix
    candidate = folder / safe_name(stem + suffix)
    i = 2
    while candidate.exists():
        candidate = folder / safe_name(f"{stem}_{i}{suffix}")
        i += 1
    return candidate


def save_uploaded_one(folder: Path, file_item: dict, fallback_name: str) -> Path:
    name = safe_name(file_item.get("filename") or fallback_name)
    path = unique_path(folder, name)
    path.write_bytes(file_item.get("data") or b"")
    return path


def prepare_upload_payload(fields: dict, files: dict) -> dict:
    payload = dict(fields)
    upload_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    base = UPLOADS_DIR / upload_id
    base.mkdir(parents=True, exist_ok=True)

    source_files = files.get("source_file") or []
    source_path = str(fields.get("source_path", "")).strip()
    if source_files:
        payload["source"] = str(save_uploaded_one(base / "source", source_files[0], "source.mp4"))
    elif source_path:
        payload["source"] = source_path

    script_files = files.get("script_file") or []
    if not str(payload.get("script_text", "")).strip() and script_files:
        payload["script_path"] = str(save_uploaded_one(base / "script", script_files[0], "script.txt"))

    bgm_files = files.get("bgm_file") or []
    bgm_path = str(fields.get("bgm_path", "")).strip()
    if bgm_files:
        payload["bgm"] = str(save_uploaded_one(base / "bgm", bgm_files[0], "bgm.mp3"))
    elif bgm_path:
        payload["bgm"] = bgm_path

    stock_files = files.get("stock_files") or []
    if stock_files:
        stock_dir = base / "stock"
        for item in stock_files:
            save_uploaded_one(stock_dir, item, "stock.mp4")
        payload["stock_dir"] = str(stock_dir)

    vo_files = files.get("vo_files") or []
    if vo_files:
        vo_dir = base / "vo"
        for item in vo_files:
            save_uploaded_one(vo_dir, item, "001.wav")
        payload["vo_dir"] = str(vo_dir)

    font_files = files.get("font_file") or []
    if font_files:
        payload["font"] = str(save_uploaded_one(base / "font", font_files[0], "font.ttf"))

    return payload


def run_process(job: Job, cmd: Sequence[str], cwd: Path = APP_DIR) -> None:
    job.add("Menjalankan: " + " ".join(shlex.quote(str(x)) for x in cmd))
    proc = subprocess.Popen(
        [str(x) for x in cmd], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        job.add(line)
    rc = proc.wait()
    job.returncode = rc
    if rc != 0:
        raise RuntimeError(f"Proses berhenti dengan kode {rc}")


def create_job(target, *args, **kwargs) -> Job:
    job = Job(uuid.uuid4().hex[:10])
    with JOBS_LOCK:
        JOBS[job.id] = job

    def runner() -> None:
        try:
            target(job, *args, **kwargs)
            if job.status == "running":
                job.status = "done"
            job.add("Selesai.")
        except Exception as exc:
            job.status = "error"
            job.add(f"ERROR: {exc}")
        finally:
            job.finished_at = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return job


def render_job(job: Job, payload: dict) -> None:
    if not RENDERER.exists():
        raise RuntimeError("auto_edit_from_text.py tidak ditemukan.")
    script_text = str(payload.get("script_text", "")).strip()
    script_path_in = str(payload.get("script_path", "")).strip()
    source = str(payload.get("source", "") or payload.get("source_path", "")).strip()
    if not source:
        raise ValueError("Video sumber wajib dipilih/upload.")
    if not Path(source).expanduser().exists():
        raise ValueError(f"Video sumber tidak ditemukan: {source}")
    if not script_text and not script_path_in:
        raise ValueError("Isi teks arahan atau path file script wajib diisi.")

    project_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + job.id
    project = PROJECTS_DIR / project_id
    project.mkdir(parents=True, exist_ok=True)

    if script_text:
        script_path = project / "script_input.txt"
        script_path.write_text(script_text, encoding="utf-8")
    else:
        script_path = Path(script_path_in).expanduser()
        if not script_path.exists():
            raise ValueError(f"File script tidak ditemukan: {script_path}")

    out_name = safe_name(str(payload.get("out_name", "final_auto_edit.mp4")))
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_path = project / out_name
    job.output_path = str(out_path)

    cmd: List[str] = [
        sys.executable, str(RENDERER),
        "--script", str(script_path),
        "--source", source,
        "--out", str(out_path),
        "--size", str(payload.get("size", "auto") or "auto"),
        "--fps", str(int(payload.get("fps", 30) or 30)),
        "--micro-seconds", str(float(payload.get("micro_seconds", 4.5) or 4.5)),
        "--bgm-volume", str(float(payload.get("bgm_volume", 0.13) or 0.13)),
        "--tts", str(payload.get("tts", "edge") or "edge"),
        "--edge-voice", str(payload.get("edge_voice", "id-ID-ArdiNeural") or "id-ID-ArdiNeural"),
        "--edge-rate", str(payload.get("edge_rate", "+0%") or "+0%"),
    ]
    optional_map = [
        ("stock_dir", "--stock-dir"),
        ("bgm", "--bgm"),
        ("vo_dir", "--vo-dir"),
        ("font", "--font"),
        ("max_scene_duration", "--max-scene-duration"),
    ]
    for key, flag in optional_map:
        val = str(payload.get(key, "")).strip()
        if val:
            cmd.extend([flag, val])
    if payload.get("no_intro"):
        cmd.append("--no-intro")
    if payload.get("no_outro"):
        cmd.append("--no-outro")
    if payload.get("no_auto_install"):
        cmd.append("--no-auto-install")
    run_process(job, cmd)


def install_job(job: Job) -> None:
    if is_termux() and which("pkg"):
        run_process(job, ["bash", "-lc", "pkg update -y && pkg install -y python ffmpeg unzip"], cwd=APP_DIR)
    run_process(job, [sys.executable, "-m", "pip", "install", "--upgrade", "edge-tts"], cwd=APP_DIR)


def list_dir(path_text: str, kind: str = "video") -> dict:
    p = Path(path_text or "/sdcard").expanduser()
    if not p.exists() or not p.is_dir():
        p = Path("/sdcard") if Path("/sdcard").exists() else Path.home()
    exts = VIDEO_EXTS if kind == "video" else (AUDIO_EXTS if kind == "audio" else TEXT_EXTS)
    entries = []
    try:
        for x in sorted(p.iterdir(), key=lambda q: (not q.is_dir(), q.name.lower()))[:800]:
            if x.name.startswith("."):
                continue
            if x.is_dir():
                entries.append({"name": x.name + "/", "path": str(x), "type": "dir"})
            elif x.suffix.lower() in exts:
                entries.append({"name": x.name, "path": str(x), "type": "file"})
    except OSError as exc:
        return {"path": str(p), "error": str(exc), "entries": []}
    roots = [str(Path.home())]
    if Path("/sdcard").exists():
        roots = ["/sdcard", "/sdcard/Download", "/sdcard/Movies", "/sdcard/DCIM"] + roots
    parent = str(p.parent) if p.parent != p else str(p)
    return {"path": str(p), "parent": parent, "roots": roots, "entries": entries}


HTML = r'''<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auto Edit Video dari Teks</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#0b1020;color:#eaf0ff}
body{margin:0;padding:18px}.wrap{max-width:1120px;margin:auto}.card{background:#111936;border:1px solid #263152;border-radius:18px;padding:16px;margin:14px 0;box-shadow:0 8px 32px rgba(0,0,0,.22)}
h1{font-size:25px;margin:0 0 8px}h2{margin:0 0 10px}.muted{color:#aab6d3}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:820px){.row{grid-template-columns:1fr}}
label{display:block;font-weight:800;margin:10px 0 6px}input,textarea,select,button{font:inherit;border-radius:12px;border:1px solid #34405f;background:#0d1430;color:#eaf0ff;padding:11px;width:100%;box-sizing:border-box}input[type=file]{background:#162044;border-style:dashed}textarea{min-height:330px;line-height:1.42}button{background:#3a63ff;border:0;font-weight:900;cursor:pointer}.secondary{background:#1c2748}.small{font-size:13px}pre{white-space:pre-wrap;background:#070b18;border-radius:14px;padding:12px;max-height:420px;overflow:auto}.badge{display:inline-block;border:1px solid #3b4a70;border-radius:999px;padding:5px 9px;margin:3px;color:#cfd8f5}.ok{color:#84f1b1}.bad{color:#ff9fae}.path{word-break:break-all;color:#cfd8f5}.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}@media(max-width:920px){.grid3{grid-template-columns:1fr}}
.step{border:1px solid #29365a;border-radius:16px;padding:12px;background:#0d1430;margin:10px 0}.step b{display:block;margin-bottom:6px}.hint{font-size:13px;color:#aab6d3;margin-top:6px}details{margin:8px 0}summary{cursor:pointer;color:#cfd8f5}.download{display:inline-block;margin-top:10px;background:#1c2748;color:#eaf0ff;padding:9px 12px;border-radius:11px;text-decoration:none;font-weight:800}
</style>
</head>
<body><div class="wrap">
<div class="card"><h1>Auto Edit Video dari Teks</h1><div class="muted">Pilih file langsung dari tombol upload. Tidak perlu mengetik path video lagi. Subtitle default sekarang per-kata dengan efek pop-up kecil di tengah-bawah.</div><div id="badges"></div></div>
<div class="card">
  <div class="row"><div>
    <label>Teks arahan / script</label>
    <textarea id="script_text" placeholder="Paste script blok di sini:&#10;00:00–01:20&#10;FORMULA EDITING: ...&#10;AUDIO/BGM: ...&#10;NARASI: &quot;...&quot;"></textarea>
    <div class="step">
      <b>Atau upload file script .txt</b>
      <input id="script_file" type="file" accept=".txt,text/plain">
      <div class="hint">Kalau kotak teks di atas diisi, file script ini diabaikan.</div>
    </div>
  </div><div>
    <div class="step">
      <b>1. Pilih video sumber</b>
      <input id="source_file" type="file" accept="video/*,.mp4,.mkv,.mov,.webm,.m4v,.avi,.3gp">
      <div class="hint">Ini cara utama. File akan disalin otomatis ke folder kerja aplikasi.</div>
      <details><summary>Path manual opsional untuk file sangat besar</summary><input id="source_path" placeholder="/sdcard/Movies/video.mp4"></details>
    </div>
    <div class="grid3">
      <div><label>Resolusi</label><select id="size"><option value="auto" selected>Auto - ikuti video asli</option><option>1280x720</option><option>1920x1080</option><option>1080x1920</option><option>720x960</option></select></div>
      <div><label>FPS</label><input id="fps" value="30" inputmode="numeric"></div>
      <div><label>Micro cut default</label><input id="micro_seconds" value="4.5" inputmode="decimal"></div>
    </div>
    <div class="step">
      <b>2. File tambahan opsional</b>
      <label>BGM</label><input id="bgm_file" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg,.flac">
      <details><summary>Path BGM manual opsional</summary><input id="bgm_path" placeholder="/sdcard/Music/bgm.mp3"></details>
      <label>Stock footage/gambar, boleh pilih banyak</label><input id="stock_files" type="file" multiple accept="video/*,image/*,.mp4,.mkv,.mov,.webm,.jpg,.jpeg,.png,.webp">
      <label>VO manual, boleh pilih banyak</label><input id="vo_files" type="file" multiple accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg,.flac">
      <label>Font .ttf/.otf opsional</label><input id="font_file" type="file" accept=".ttf,.otf,font/*">
    </div>
    <label>Nama output</label><input id="out_name" value="final_auto_edit.mp4">
    <div class="grid3">
      <div><label>Mode VO</label><select id="tts"><option value="edge">edge-tts Ardi</option><option value="none">VO folder / silent</option></select></div>
      <div><label>Voice</label><select id="edge_voice"><option value="id-ID-ArdiNeural">Indonesia Ardi</option><option value="id-ID-GadisNeural">Indonesia Gadis</option></select></div>
      <div><label>Rate TTS</label><input id="edge_rate" value="+0%"></div>
    </div>
    <div class="muted small" style="margin:10px 0">Intro dan outro teks tetap dimatikan otomatis.</div>
    <button onclick="startRender()">Render Otomatis Sampai Final</button>
    <button class="secondary" onclick="installTools()" style="margin-top:8px">Install / perbaiki alat</button>
  </div></div>
</div>
<div class="card"><h2>Log</h2><div id="result" class="muted">Belum ada proses.</div><pre id="logs"></pre></div>
</div>
<script>
const el=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function getStatus(){const r=await fetch('/api/status');const d=await r.json();el('badges').innerHTML=`<span class="badge">ffmpeg: <b class="${d.ffmpeg?'ok':'bad'}">${d.ffmpeg?'ada':'belum'}</b></span><span class="badge">ffprobe: <b class="${d.ffprobe?'ok':'bad'}">${d.ffprobe?'ada':'belum'}</b></span><span class="badge">edge-tts: <b class="${d.edge_tts?'ok':'bad'}">${d.edge_tts?'ada':'belum'}</b></span><span class="badge">Termux: ${d.is_termux?'ya':'tidak'}</span><span class="badge">${esc(d.app_dir)}</span>`;}
function appendFile(fd,id,name){const f=el(id).files[0]; if(f) fd.append(name,f,f.name);}
function appendFiles(fd,id,name){for(const f of el(id).files){fd.append(name,f,f.webkitRelativePath||f.name);}}
function formDataPayload(){const fd=new FormData();
  for(const id of ['script_text','source_path','size','fps','micro_seconds','bgm_path','out_name','tts','edge_voice','edge_rate']) fd.append(id,el(id).value);
  fd.append('no_intro','true'); fd.append('no_outro','true');
  appendFile(fd,'source_file','source_file'); appendFile(fd,'script_file','script_file'); appendFile(fd,'bgm_file','bgm_file'); appendFile(fd,'font_file','font_file');
  appendFiles(fd,'stock_files','stock_files'); appendFiles(fd,'vo_files','vo_files');
  return fd;
}
async function startRender(){
  el('result').textContent='Mengupload file dan memulai render...'; el('logs').textContent='';
  const hasSource=el('source_file').files.length>0 || el('source_path').value.trim();
  const hasScript=el('script_text').value.trim() || el('script_file').files.length>0;
  if(!hasSource){el('result').innerHTML='<span class="bad">Pilih video sumber dulu.</span>';return;}
  if(!hasScript){el('result').innerHTML='<span class="bad">Isi teks script atau upload file .txt dulu.</span>';return;}
  const r=await fetch('/api/render-upload',{method:'POST',body:formDataPayload()}); const d=await r.json();
  if(!r.ok){el('result').innerHTML='<span class="bad">'+esc(d.error||'gagal')+'</span>';return;} poll(d.id);
}
async function installTools(){el('result').textContent='Memulai instalasi/perbaikan alat...';const r=await fetch('/api/install',{method:'POST'});const d=await r.json();poll(d.id);}
async function poll(id){const r=await fetch('/api/job?id='+encodeURIComponent(id));const d=await r.json();el('logs').textContent=(d.logs||[]).join('\n');if(d.status==='done'){let link=d.download_url?`<br><a class="download" href="${esc(d.download_url)}">Download video</a>`:'';el('result').innerHTML='<span class="ok">Selesai.</span> Output: <span class="path">'+esc(d.output_path||'')+'</span>'+link;}else if(d.status==='error'){el('result').innerHTML='<span class="bad">Error.</span> Cek log.';}else{el('result').textContent='Sedang proses...';setTimeout(()=>poll(id),1200);}}
getStatus();
</script>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoTextEdit/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            text_response(self, HTML, ctype="text/html; charset=utf-8")
        elif parsed.path == "/api/status":
            json_response(self, status_data())
        elif parsed.path == "/api/job":
            qs = parse_qs(parsed.query)
            jid = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if not job:
                json_response(self, {"error": "job tidak ditemukan"}, 404)
            else:
                download_url = f"/api/download?id={quote(job.id)}" if job.status == "done" and job.output_path and Path(job.output_path).exists() else ""
                json_response(self, {"id": job.id, "status": job.status, "logs": job.logs, "output_path": job.output_path, "download_url": download_url, "returncode": job.returncode})
        elif parsed.path == "/api/download":
            qs = parse_qs(parsed.query)
            jid = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if not job or not job.output_path or not Path(job.output_path).exists():
                text_response(self, "File output tidak ditemukan", 404)
            else:
                path = Path(job.output_path)
                ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition", f"attachment; filename={path.name}")
                self.end_headers()
                with path.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile)
        elif parsed.path == "/api/browse":
            qs = parse_qs(parsed.query)
            path = unquote((qs.get("path") or ["/sdcard"])[0])
            kind = (qs.get("kind") or ["video"])[0]
            json_response(self, list_dir(path, kind))
        else:
            text_response(self, "Not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/render":
                payload = read_json(self)
                job = create_job(render_job, payload)
                json_response(self, {"id": job.id})
            elif parsed.path == "/api/render-upload":
                fields, files = read_multipart(self)
                payload = prepare_upload_payload(fields, files)
                job = create_job(render_job, payload)
                json_response(self, {"id": job.id})
            elif parsed.path == "/api/install":
                job = create_job(install_job)
                json_response(self, {"id": job.id})
            else:
                text_response(self, "Not found", 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 400)


def serve(host: str, port: int, open_browser: bool) -> None:
    last_error = None
    for p in range(port, port + 30):
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            url = f"http://{host}:{p}/"
            print(f"Web aktif: {url}", flush=True)
            if open_browser:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            httpd.serve_forever()
            return
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"Gagal membuka port mulai {port}: {last_error}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    serve(args.host, args.port, args.open)


if __name__ == "__main__":
    main()
