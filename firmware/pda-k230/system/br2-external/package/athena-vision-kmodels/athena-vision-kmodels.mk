################################################################################
#
# athena-vision-kmodels
#
################################################################################

# The archive the K230 SDK's ai_demo "common" package downloads (same site, name
# and hash: package/ai_demo/common/common.mk at k230_linux_sdk fc6cc21). It shares
# that package's download directory, so it is fetched once. Only the two models
# are extracted (the archive holds every ai_demo model).
ATHENA_VISION_KMODELS_VERSION = 2.10.0
ATHENA_VISION_KMODELS_SITE = https://download.kendryte.com/k230/downloads/kmodel
ATHENA_VISION_KMODELS_SOURCE = kmodel_v$(ATHENA_VISION_KMODELS_VERSION).tgz
ATHENA_VISION_KMODELS_DL_SUBDIR = common
# Canaan's archive states no license.
ATHENA_VISION_KMODELS_LICENSE = PROPRIETARY
ATHENA_VISION_KMODELS_REDISTRIBUTE = NO

ATHENA_VISION_KMODELS_FILES = hand_det.kmodel handkp_det.kmodel

define ATHENA_VISION_KMODELS_EXTRACT_CMDS
	mkdir -p $(@D)/archive
	$(call suitable-extractor,$(ATHENA_VISION_KMODELS_SOURCE)) \
		$(ATHENA_VISION_KMODELS_DL_DIR)/$(ATHENA_VISION_KMODELS_SOURCE) | \
		$(TAR) -C $(@D)/archive $(TAR_OPTIONS) - --wildcards \
			$(foreach f,$(ATHENA_VISION_KMODELS_FILES),'*/$(f)')
endef

# The SDK's ai_demo programs install them from kmodel/ai_poc/kmodel/ in the archive.
define ATHENA_VISION_KMODELS_INSTALL_TARGET_CMDS
	$(foreach f,$(ATHENA_VISION_KMODELS_FILES),\
		src=`find $(@D)/archive -path '*ai_poc/kmodel/$(f)' | head -n 1`; \
		test -n "$$src" || { echo "$(f) not in $(ATHENA_VISION_KMODELS_SOURCE)"; exit 1; }; \
		$(INSTALL) -D -m 0644 "$$src" \
			$(TARGET_DIR)/usr/share/athena-vision/models/$(f)$(sep))
endef

$(eval $(generic-package))
