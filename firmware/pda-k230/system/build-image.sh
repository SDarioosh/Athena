#!/usr/bin/env bash
# build-image.sh: build the Athena PDA's SD-card image with LilyGO's T-Display-K230
# tree (https://github.com/Xinyuan-LilyGO/T-Display-K230, its k230_linux_sdk
# submodule at fc6cc21, Buildroot 2025.02.1) and this repository's br2-external tree.
#
#   firmware/pda-k230/system/build-image.sh /path/to/t-display-k230
#
# Steps (each is also what you would type by hand):
#   1. LilyGO's scripts/apply_to_sdk.sh: their BSP overlay and launcher into the SDK
#      (also writes BR2_PACKAGE_K230_PHONE_UI=y into their defconfig).
#   2. gen-defconfig.sh: their defconfig + br2-external/board/defconfig.fragment ->
#      <sdk>/buildroot-overlay/configs/$CONF (a defconfig of its own, so LilyGO's
#      stays as it is and its output directory is not touched).
#   3. make -C <sdk> CONF=$CONF $CONF, then all, with BR2_EXTERNAL set (Buildroot
#      keeps it in output/$CONF/.br2-external.mk for later runs).
#   4. LilyGO's k230_bsp/scripts/export_images.sh -> $EXPORT_DIR.
#
# Environment: SDK_DIR (default <lilygo>/k230_linux_sdk), CONF (default
# k230_canmv_t_display_athena_pda_defconfig), EXPORT_DIR (default
# <lilygo>/k230_bsp/images-athena-pda), SKIP_APPLY=1 to skip step 1.
# Needs the host set up by LilyGO's scripts/setup_ubuntu.sh (the Xuantie toolchain
# in /opt/toolchain). A first build takes hours and ~50 GB.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT="$HERE/br2-external"
BASE_CONF=k230_canmv_t_display_rm69a10_defconfig
EXPECTED_SDK=fc6cc2151d54cb307be47f81815023f7ffb0eae6

if [ $# -lt 1 ] || [ ! -d "$1" ]; then
	sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2
	exit 2
fi
LILYGO_DIR="$(cd "$1" && pwd)"
SDK_DIR="${SDK_DIR:-$LILYGO_DIR/k230_linux_sdk}"
CONF="${CONF:-k230_canmv_t_display_athena_pda_defconfig}"
EXPORT_DIR="${EXPORT_DIR:-$LILYGO_DIR/k230_bsp/images-athena-pda}"
# CONF names a file in the SDK's configs directory and Buildroot's output directory: a
# plain name, never a path
case "$CONF" in
	''|.*|*[!A-Za-z0-9_.-]*) echo "CONF must be a plain defconfig name, not '$CONF'" >&2; exit 2 ;;
esac

for f in "$LILYGO_DIR/scripts/apply_to_sdk.sh" "$LILYGO_DIR/k230_bsp/scripts/export_images.sh"; do
	[ -x "$f" ] || { echo "not LilyGO's T-Display-K230 tree: no $f" >&2; exit 1; }
done
[ -f "$SDK_DIR/Makefile" ] || {
	echo "no SDK in $SDK_DIR (git submodule update --init --recursive)" >&2
	exit 1
}
sdk_commit="$(git -C "$SDK_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
if [ "$sdk_commit" != "$EXPECTED_SDK" ]; then
	echo "warning: SDK at $sdk_commit; this tree was written against $EXPECTED_SDK" >&2
fi

if [ "${SKIP_APPLY:-0}" != 1 ]; then
	echo "== 1. LilyGO's overlay and launcher into the SDK"
	"$LILYGO_DIR/scripts/apply_to_sdk.sh"
fi

echo "== 2. $CONF"
base="$SDK_DIR/buildroot-overlay/configs/$BASE_CONF"
out="$SDK_DIR/buildroot-overlay/configs/$CONF"
[ -f "$base" ] || { echo "no $base: did step 1 run?" >&2; exit 1; }
"$HERE/gen-defconfig.sh" "$base" "$EXT/board/defconfig.fragment" >"$out.tmp"
mv "$out.tmp" "$out"
echo "wrote $out"

echo "== 3. Buildroot ($CONF, BR2_EXTERNAL=$EXT)"
export BR2_EXTERNAL="$EXT"
make -C "$SDK_DIR" CONF="$CONF" "$CONF"
make -C "$SDK_DIR" CONF="$CONF" all

echo "== 4. Export"
"$LILYGO_DIR/k230_bsp/scripts/export_images.sh" "$SDK_DIR" "$EXPORT_DIR" "$CONF"
echo "Image: $EXPORT_DIR/sysimage-sdcard.img"
