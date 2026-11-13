#!/bin/sh
# Athena PDA post-build step: runs after LilyGO's post-build.sh
# (BR2_ROOTFS_POST_BUILD_SCRIPT, see defconfig.fragment). $1 is the target directory.
#
# LilyGO's launcher starts from /etc/init.d/S99zz_k230_phone_ui (k230_launcher's
# rootfs_overlay). The PDA starts in its place (docs/k230-pda-plan.md, open
# question 3), so the script is moved out of rcS's way, unchanged, and S92athena-pda
# starts it instead when /etc/default/athena-pda says ATHENA_PDA_BOOT_UI=launcher.
# The launcher itself (/root/app/k230_phone_ui) stays installed.
set -eu

target="${1:?usage: post-build.sh <target dir>}"
launcher_init="$target/etc/init.d/S99zz_k230_phone_ui"
kept="$target/usr/libexec/athena-pda/k230_phone_ui.init"

if [ -f "$launcher_init" ]; then
	mkdir -p "$(dirname "$kept")"
	mv -f "$launcher_init" "$kept"
	chmod 0755 "$kept"
	echo "athena-pda: launcher boot script moved to /usr/libexec/athena-pda/k230_phone_ui.init"
elif [ ! -f "$kept" ]; then
	echo "athena-pda: warning: no launcher boot script (S99zz_k230_phone_ui) found;" \
		"ATHENA_PDA_BOOT_UI=launcher will not work" >&2
fi
