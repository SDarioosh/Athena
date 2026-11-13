################################################################################
#
# athena-pda-system
#
################################################################################

ATHENA_PDA_SYSTEM_VERSION = 1
ATHENA_PDA_SYSTEM_SITE = $(BR2_EXTERNAL_ATHENA_PDA_PATH)/package/athena-pda-system/files
ATHENA_PDA_SYSTEM_SITE_METHOD = local
ATHENA_PDA_SYSTEM_LICENSE = PROPRIETARY

# The UI and syncd run as this user (video: DRM and backlight, input: touch).
define ATHENA_PDA_SYSTEM_USERS
	athena-pda -1 athena-pda -1 * /var/lib/athena-pda - video,input Athena PDA
endef

define ATHENA_PDA_SYSTEM_PERMISSIONS
	/var/lib/athena-pda d 700 athena-pda athena-pda - - - - -
endef

ATHENA_PDA_SYSTEM_BINS = athena-wifi athena-board athena-tailnet athena-boot-ui

define ATHENA_PDA_SYSTEM_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0644 $(@D)/athena-pda.default \
		$(TARGET_DIR)/etc/default/athena-pda
	$(INSTALL) -D -m 0644 $(@D)/athena-pda-common.sh \
		$(TARGET_DIR)/usr/libexec/athena-pda/athena-pda-common.sh
	$(INSTALL) -D -m 0755 $(@D)/athena-supervise \
		$(TARGET_DIR)/usr/libexec/athena-pda/athena-supervise
	$(INSTALL) -D -m 0755 $(@D)/tailscale-wrapper \
		$(TARGET_DIR)/usr/libexec/athena-pda/bin/tailscale
	$(foreach f,$(ATHENA_PDA_SYSTEM_BINS),\
		$(INSTALL) -D -m 0755 $(@D)/$(f) $(TARGET_DIR)/usr/bin/$(f)$(sep))
endef

define ATHENA_PDA_SYSTEM_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(@D)/S42athena-wifi \
		$(TARGET_DIR)/etc/init.d/S42athena-wifi
endef

$(eval $(generic-package))
