################################################################################
#
# athena-vision
#
################################################################################

ATHENA_VISION_VERSION = local
ATHENA_VISION_SITE = $(ATHENA_PDA_FW_DIR)
ATHENA_VISION_SITE_METHOD = local
ATHENA_VISION_SUBDIR = pda-k230/vision
ATHENA_VISION_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
	$(ATHENA_PDA_RSYNC_JUNK) \
	--include=/pda-k230/ --include=/pda-k230/vision/*** \
	--exclude=*
ATHENA_VISION_SUPPORTS_IN_SOURCE_BUILD = NO
ATHENA_VISION_LICENSE = PROPRIETARY
ATHENA_VISION_DEPENDENCIES = \
	libnncase libmmz gsl-lite libdrm vvcam display zxing-cpp athena-vision-kmodels

# VISION_DEVICE: the nncase/V4L2 program. Tests and the replay tools stay on the PC.
# The Xuantie C908 flags come from BR2_TARGET_OPTIMIZATION already; the program
# adds them again with VISION_K230_CPU_FLAGS (same values, harmless).
ATHENA_VISION_CONF_OPTS = \
	-DVISION_DEVICE=ON \
	-DVISION_TESTS=OFF \
	-DVISION_TOOLS=OFF \
	-DVISION_WERROR=OFF \
	-DVISION_QR=zxing

# Settings: /etc/athena-pda/vision.conf, the program's own file, written from its
# vision.conf.example (all defaults). Models: athena-vision-kmodels in
# /usr/share/athena-vision/models, the program's default model_dir. The face model
# (presence) is LilyGO's: /root/app/face_detect (BR2_PACKAGE_FACE_DETECT).
define ATHENA_VISION_INSTALL_CONF
	$(INSTALL) -D -m 0644 $(@D)/pda-k230/vision/vision.conf.example \
		$(TARGET_DIR)/etc/athena-pda/vision.conf
endef
ATHENA_VISION_POST_INSTALL_TARGET_HOOKS += ATHENA_VISION_INSTALL_CONF

define ATHENA_VISION_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(ATHENA_VISION_PKGDIR)/S94athena-vision \
		$(TARGET_DIR)/etc/init.d/S94athena-vision
endef

$(eval $(cmake-package))
