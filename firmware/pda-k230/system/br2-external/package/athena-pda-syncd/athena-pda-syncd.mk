################################################################################
#
# athena-pda-syncd
#
################################################################################

# Built from the repository: firmware/pda-k230/syncd and the BLE-free modules of
# firmware/recorder/tools/athena_relay (docs/k230-pda-plan.md, "Reused unchanged").
ATHENA_PDA_SYNCD_VERSION = local
ATHENA_PDA_SYNCD_SITE = $(ATHENA_PDA_FW_DIR)
ATHENA_PDA_SYNCD_SITE_METHOD = local
ATHENA_PDA_SYNCD_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
	$(ATHENA_PDA_RSYNC_JUNK) \
	--include=/pda-k230/ --include=/pda-k230/syncd/*** \
	--include=/recorder/ --include=/recorder/tools/ \
	--include=/recorder/tools/athena_relay/*** \
	--exclude=*
ATHENA_PDA_SYNCD_LICENSE = PROPRIETARY
ATHENA_PDA_SYNCD_DEPENDENCIES = python3 host-python3

ATHENA_PDA_SYNCD_PYDIR = /usr/lib/athena-pda/python

# athena_relay: only what syncd imports (no ble_relay, no bleak).
ATHENA_PDA_SYNCD_RELAY_MODULES = \
	__init__ anchors drain protocol uploader \
	pda_actions pda_calendar pda_feed pda_insight pda_protocol pda_sync pda_todos

define ATHENA_PDA_SYNCD_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)
	mkdir -p $(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)/athena_pda_sync \
		$(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)/athena_relay
	$(INSTALL) -m 0644 $(@D)/pda-k230/syncd/athena_pda_sync/*.py \
		$(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)/athena_pda_sync/
	$(foreach m,$(ATHENA_PDA_SYNCD_RELAY_MODULES),\
		$(INSTALL) -m 0644 $(@D)/recorder/tools/athena_relay/$(m).py \
			$(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)/athena_relay/$(m).py$(sep))
	$(INSTALL) -m 0755 $(@D)/pda-k230/syncd/athena-pda-syncd \
		$(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)/athena-pda-syncd
	ln -sf ../lib/athena-pda/python/athena-pda-syncd \
		$(TARGET_DIR)/usr/bin/athena-pda-syncd
	$(HOST_DIR)/bin/python3 -m compileall -q -j 0 \
		-d $(ATHENA_PDA_SYNCD_PYDIR) $(TARGET_DIR)$(ATHENA_PDA_SYNCD_PYDIR)
endef

define ATHENA_PDA_SYNCD_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(ATHENA_PDA_SYNCD_PKGDIR)/S93athena-pda-syncd \
		$(TARGET_DIR)/etc/init.d/S93athena-pda-syncd
endef

$(eval $(generic-package))
