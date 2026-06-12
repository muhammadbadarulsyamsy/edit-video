#!/data/data/com.termux/files/usr/bin/bash
# Jalankan dengan: bash mulai_web.sh
set -u
cd "$(dirname "$0")"
info(){ printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
info "Memeriksa alat Termux..."
if command -v termux-setup-storage >/dev/null 2>&1 && [ ! -d /sdcard ]; then
  info "Meminta izin storage Android. Ketuk Izinkan jika muncul."
  termux-setup-storage || true
  sleep 1
fi
if ! command -v pkg >/dev/null 2>&1; then
  echo "pkg tidak ditemukan. Jalankan dari Termux resmi."
  exit 1
fi
if ! command -v python >/dev/null 2>&1; then
  info "Menginstall Python..."
  pkg update -y && pkg install -y python
fi
MISSING=""
if ! command -v ffmpeg >/dev/null 2>&1; then MISSING="$MISSING ffmpeg"; fi
if ! command -v ffprobe >/dev/null 2>&1; then MISSING="$MISSING ffmpeg"; fi
if ! command -v unzip >/dev/null 2>&1; then MISSING="$MISSING unzip"; fi
MISSING="$(printf '%s\n' $MISSING | awk '!seen[$0]++' | tr '\n' ' ')"
if [ -n "$MISSING" ]; then
  info "Menginstall alat wajib:$MISSING"
  pkg update -y
  # shellcheck disable=SC2086
  pkg install -y $MISSING
fi
if ! python -c 'import edge_tts' >/dev/null 2>&1; then
  info "Menginstall edge-tts untuk VO Indonesia Ardi. Internet diperlukan."
  python -m pip install --upgrade edge-tts || info "edge-tts gagal dipasang sekarang. Web tetap bisa berjalan, mode VO akan silent/VO manual."
fi
mkdir -p projects uploads stock voice bgm output
info "Menjalankan web. Jika browser tidak terbuka, salin URL yang muncul."
python run.py --host 127.0.0.1 --port 7860 --open
