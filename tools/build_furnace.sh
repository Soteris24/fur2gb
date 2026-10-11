#!/bin/sh
# Build the reference Furnace: the release the songs are written in (0.6.8.2) plus a small patch
# that writes its GB backend's register writes to $FUR2GB_GB_TRACE (tools/furnace_trace.patch).
# Needs git and MSYS2 UCRT64 (cmake, ninja, gcc). Output: .build/furnace-0682-build/furnace.exe
# USE_BACKWARD=OFF: its -gcodeview makes GCC 16 hang on some files (dispatchContainer.cpp, ...).
set -e
cd "$(dirname "$0")/.."
VERSION=v0.6.8.2
SRC=.build/furnace-0682
OUT=.build/furnace-0682-build
export PATH="/c/msys64/ucrt64/bin:$PATH"

if [ ! -d "$SRC" ]; then
  git clone --depth 1 --branch "$VERSION" --recurse-submodules --shallow-submodules \
    https://github.com/tildearrow/furnace.git "$SRC"
  git -C "$SRC" apply --ignore-whitespace "$PWD/tools/furnace_trace.patch"
fi
cmake -S "$SRC" -B "$OUT" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF -DUSE_SDL2=OFF \
  -DWITH_PORTAUDIO=OFF -DWITH_JACK=OFF -DWITH_RENDER_OPENGL=OFF -DWITH_RENDER_DX11=OFF \
  -DWITH_RENDER_DX9=OFF -DWITH_RENDER_SDL=OFF -DWITH_RENDER_OPENGL1=OFF -DWITH_DEMOS=OFF \
  -DWITH_LOCALE=OFF -DSYSTEM_ZLIB=OFF -DSYSTEM_FMT=OFF -DSYSTEM_LIBSNDFILE=OFF -DSYSTEM_FFTW=OFF \
  -DSYSTEM_RTMIDI=OFF -DWITH_MPEG=OFF -DWITH_OGG=OFF -DUSE_BACKWARD=OFF
ninja -C "$OUT" furnace
