################################################################################
#
# athena-pda-ui
#
################################################################################

# Built from the repository: firmware/pda-k230/ui, which also compiles the T5's
# pure modules (firmware/pda/main) and crc32.c/isotime.c (firmware/recorder/main)
# where they are, so those directories are copied too, at the same relative paths.
ATHENA_PDA_UI_VERSION = local
ATHENA_PDA_UI_SITE = $(ATHENA_PDA_FW_DIR)
ATHENA_PDA_UI_SITE_METHOD = local
ATHENA_PDA_UI_SUBDIR = pda-k230/ui
ATHENA_PDA_UI_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
	$(ATHENA_PDA_RSYNC_JUNK) \
	--include=/pda-k230/ --include=/pda-k230/ui/*** \
	--include=/pda/ --include=/pda/main/*** \
	--include=/recorder/ --include=/recorder/main/*** \
	--exclude=*
ATHENA_PDA_UI_SUPPORTS_IN_SOURCE_BUILD = NO
ATHENA_PDA_UI_LICENSE = PROPRIETARY
ATHENA_PDA_UI_DEPENDENCIES = athena-pda-lvgl-src libdrm libevdev

# PDA_DEVICE: DRM + evdev. LVGL: the UI's CMake fetches it with FetchContent; the
# standard FETCHCONTENT_SOURCE_DIR_LVGL points it at athena-pda-lvgl-src instead (no
# network in a Buildroot build). PDA_SDL off; PDA_WERROR off (a different compiler
# may warn differently; the PC build keeps -Werror). PDA_TESTS=OFF skips the host
# tests and the screenshot tool once the UI's CMake has that option.
ATHENA_PDA_UI_CONF_OPTS = \
	-DPDA_DEVICE=ON \
	-DPDA_SDL=OFF \
	-DPDA_WERROR=OFF \
	-DPDA_TESTS=OFF \
	-DFETCHCONTENT_FULLY_DISCONNECTED=ON \
	-DFETCHCONTENT_SOURCE_DIR_LVGL=$(ATHENA_PDA_LVGL_SRC_DIR)

define ATHENA_PDA_UI_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(ATHENA_PDA_UI_PKGDIR)/S92athena-pda \
		$(TARGET_DIR)/etc/init.d/S92athena-pda
endef

$(eval $(cmake-package))
