#!/data/data/com.termux/files/usr/bin/bash
set -u
if ! command -v pkg >/dev/null 2>&1; then
  echo "pkg tidak ditemukan. Jalankan dari Termux."
  exit 1
fi
pkg update -y
pkg install -y python ffmpeg unzip
python -m pip install --upgrade edge-tts
mkdir -p projects uploads stock voice bgm output
printf '\nSelesai. Jalankan:\n  bash mulai_web.sh\n'
