#!/bin/sh
# Compiles the device layer (src/device) and the core for riscv64 without the board's
# sysroot: a type check against the real headers, objects only, no link. For the sandbox
# and CI; the real build is Buildroot's (README.md "Device build").
#
#   SDK=/path/to/k230_linux_sdk          (for v4l2-drm.h, display.h, mmz.h)
#   NNCASE_INC=/path/to/nncase_k230_v2.11.0_runtime_linux/include
#   EXTRA_INC="/path/with/gsl/gsl-lite.hpp /path/with/ZXing/ReadBarcode.h"   (dirs)
#   CXX=riscv64-linux-gnu-g++  QR=zxing|opencv|none  OUT=/tmp/athena-vision-objs
#   tools/device_compile_check.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
: "${SDK:?set SDK to a k230_linux_sdk checkout}"
: "${NNCASE_INC:?set NNCASE_INC to the nncase runtime include dir}"
CXX=${CXX:-riscv64-linux-gnu-g++}
QR=${QR:-zxing}
OUT=${OUT:-/tmp/athena-vision-objs}
mkdir -p "$OUT"
pkg="$SDK/buildroot-overlay/package"
inc="-I$here/src/core -I$here/src/net -I$here/src/device -isystem $NNCASE_INC"
inc="$inc -isystem $pkg/vvcam/v4l2-drm/include -isystem $pkg/display/include -isystem $pkg/libmmz"
inc="$inc -isystem /usr/include/libdrm"
for d in ${EXTRA_INC:-}; do inc="$inc -isystem $d"; done
defs="-DATHENA_VISION_VERSION=\"check\""
case "$QR" in
  zxing) defs="$defs -DVISION_QR_ZXING=1" ;;
  opencv) defs="$defs -DVISION_QR_OPENCV=1"; inc="$inc -isystem /usr/include/opencv4" ;;
esac
flags="-std=c++17 -O2 -Wall -Wextra -Wshadow -march=rv64gc -mabi=lp64d"
n=0
for f in "$here"/src/core/*.cpp "$here"/src/net/*.cpp "$here"/src/device/*.cpp; do
  o="$OUT/$(basename "$f" .cpp).o"
  # shellcheck disable=SC2086
  $CXX $flags $defs $inc -c "$f" -o "$o"
  n=$((n + 1))
done
echo "compiled $n files for riscv64 into $OUT"
