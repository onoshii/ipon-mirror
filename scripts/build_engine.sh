#!/usr/bin/env bash
# MSYS2 UCRT64 で受信エンジン (UxPlay + Ipon Mirror パッチ) をビルドする
# 使い方: scripts/build_engine.ps1 から呼ばれる
set -euo pipefail

UXPLAY_REPO="https://github.com/FDH2/UxPlay"
UXPLAY_COMMIT="fbd040e70e754e01fa7e6df810fecd08b6484ada"   # 動作確認済みのコミット (UxPlay 1.74)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/build/UxPlay"

pacman -S --needed --noconfirm \
  git patch \
  mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-ninja mingw-w64-ucrt-x86_64-gcc \
  mingw-w64-ucrt-x86_64-pkgconf mingw-w64-ucrt-x86_64-libplist mingw-w64-ucrt-x86_64-openssl \
  mingw-w64-ucrt-x86_64-gstreamer mingw-w64-ucrt-x86_64-gst-plugins-base \
  mingw-w64-ucrt-x86_64-gst-plugins-good mingw-w64-ucrt-x86_64-gst-plugins-bad \
  mingw-w64-ucrt-x86_64-gst-libav

if [ ! -d "$SRC/.git" ]; then
  git clone "$UXPLAY_REPO" "$SRC"
fi
cd "$SRC"
git fetch --quiet origin "$UXPLAY_COMMIT" || true
git checkout --quiet --force "$UXPLAY_COMMIT"
git reset --quiet --hard
patch -p1 --forward < "$ROOT/engine/uxplay-ipon.patch"

mkdir -p build && cd build
cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release
ninja
echo "built: $SRC/build/uxplay.exe"
