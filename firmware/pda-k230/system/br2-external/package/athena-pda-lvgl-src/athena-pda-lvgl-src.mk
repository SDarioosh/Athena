################################################################################
#
# athena-pda-lvgl-src
#
################################################################################

# The UI's CMake fetches LVGL at this commit with git on a PC. A Buildroot build has
# no network after the download step, so this package downloads the same commit
# and the UI configures with -DFETCHCONTENT_SOURCE_DIR_LVGL=$(ATHENA_PDA_LVGL_SRC_DIR).
# The file is the one LilyGO's lvgl package downloads (same name, same hash), kept
# in its download directory so it is fetched once.
ATHENA_PDA_LVGL_SRC_VERSION = 59dc7e436ae97a25e32656739ea6a943f9f11b6a
ATHENA_PDA_LVGL_SRC_SITE = $(call github,lvgl,lvgl,$(ATHENA_PDA_LVGL_SRC_VERSION))
ATHENA_PDA_LVGL_SRC_SOURCE = lvgl-$(ATHENA_PDA_LVGL_SRC_VERSION).tar.gz
ATHENA_PDA_LVGL_SRC_DL_SUBDIR = lvgl
ATHENA_PDA_LVGL_SRC_LICENSE = MIT
ATHENA_PDA_LVGL_SRC_LICENSE_FILES = LICENCE.txt
ATHENA_PDA_LVGL_SRC_INSTALL_TARGET = NO

$(eval $(generic-package))
