EDITVIDEO AUTO TEXT FINAL
=========================

Fungsi utama:
- Anda hanya memberi teks arahan/script dan video sumber.
- Kode membaca timestamp, FORMULA EDITING, AUDIO/BGM, dan NARASI.
- Kode otomatis membuat VO, memotong video per timestamp, membuat micro-cut ±3-5 detik, memberi efek sesuai keyword, menambah subtitle, mute audio asli, mencampur BGM opsional, lalu menghasilkan MP4 final.
- Resolusi/rasio output default sekarang AUTO mengikuti video sumber, jadi video 3:4 tetap keluar 3:4 dan tidak dipaksa 16:9.
- Intro dan outro teks di awal/akhir sekarang default MATI.

FORMAT SCRIPT YANG DIDUKUNG
===========================

Format blok multiline:

00:00–01:20
FORMULA EDITING: Gunakan hook atmosferik. Pola tiap ±4–5 detik: ZOOM 112%, GRADE gelap, film grain, STOCK [layar digital gelap].
AUDIO/BGM: MUTE audio asli. BGM berlisensi dark ambient.
NARASI: "Tulis narasi voice over di sini."

Format lama satu baris juga didukung:

00:00–00:14 [CUT 3–5s, VO ORISINAL, MUTE AUDIO ASLI, GRADE GELAP] → Narasi: "Tulis narasi di sini."

CARA PAKAI TERMUX WEB
=====================

1. Ekstrak folder ini ke /sdcard, misalnya:

   /sdcard/editvideo_auto_text_final

2. Buka Termux, lalu jalankan:

   cd /sdcard/editvideo_auto_text_final
   bash mulai_web.sh

3. Buka URL yang muncul, biasanya:

   http://127.0.0.1:7860/

4. Di web:
   - Paste teks arahan ke kolom script, atau isi path file script .txt.
   - Isi path video sumber, atau pilih lewat tombol Buka Direktori HP.
   - Isi BGM/stock/VO jika ada.
   - Tekan Render Otomatis Sampai Final.

5. Output final tersimpan di folder projects/<tanggal_job>/final_auto_edit.mp4.

CARA PAKAI CLI
==============

Contoh paling sederhana:

python auto_edit_from_text.py \
  --script /sdcard/Download/script.txt \
  --source /sdcard/Movies/video.mp4 \
  --out /sdcard/Movies/final_auto_edit.mp4

Resolusi default adalah auto mengikuti video sumber. Jika ingin memaksa ukuran tertentu, tambahkan contoh:

python auto_edit_from_text.py \
  --script /sdcard/Download/script.txt \
  --source /sdcard/Movies/video_3x4.mp4 \
  --size 720x960 \
  --out /sdcard/Movies/final_3x4.mp4

Dengan BGM dan stock:

python auto_edit_from_text.py \
  --script /sdcard/Download/script.txt \
  --source /sdcard/Movies/video.mp4 \
  --stock-dir /sdcard/Download/stock \
  --bgm /sdcard/Music/bgm_legal.mp3 \
  --out /sdcard/Movies/final_auto_edit.mp4

Preview parser tanpa render:

python auto_edit_from_text.py --script /sdcard/Download/script.txt --preview

DEPENDENSI
==========

Termux:

pkg install python ffmpeg unzip
python -m pip install --upgrade edge-tts

edge-tts membutuhkan internet saat membuat voice over. Jika internet tidak aktif, renderer tetap lanjut memakai VO manual dari folder --vo-dir, atau membuat audio silent fallback.

NAMA FILE VO MANUAL
===================

Jika memakai folder VO sendiri, simpan dengan nama seperti:

001.wav
002.wav
003.wav

atau:

scene_001.wav
scene_002.wav
scene_003.wav

EFEK YANG DIBACA DARI TEKS
==========================

Keyword yang dikenali otomatis:
- ZOOM 110%, ZOOM 112%, ZOOM 115%.
- GRADE gelap, dingin/biru, hangat, merah, kontras, desaturasi/pucat, overexposed.
- film grain, noise, glitch.
- border/bingkai.
- stock/cutaway/visual pengganti.
- speed up, slow, SPEED x1.2.
- tiap ±4-5 detik / cut 2-3 detik untuk durasi micro-cut.

CATATAN PENTING
===============

Gunakan hanya video, musik, stock footage, font, dan aset lain yang Anda miliki haknya atau berlisensi. Audio asli video sumber dimute secara default. Intro/outro teks tidak ditambahkan kecuali menjalankan CLI dengan --with-intro atau --with-outro. Kode ini dibuat untuk workflow recap/komentar transformatif dengan narasi orisinal, bukan untuk menghindari Content ID atau sistem klaim hak cipta.
