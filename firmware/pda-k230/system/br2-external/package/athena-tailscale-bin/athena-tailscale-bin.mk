################################################################################
#
# athena-tailscale-bin
#
################################################################################

# Tailscale's static build. To update: pick the version from
# https://pkgs.tailscale.com/stable/?mode=json ("Tarballs": "riscv64"), set it here,
# and put the .tgz's sha256 (the .tgz.sha256 file next to it) in the .hash file.
ATHENA_TAILSCALE_BIN_VERSION = 1.104.7
ATHENA_TAILSCALE_BIN_SITE = https://pkgs.tailscale.com/stable
ATHENA_TAILSCALE_BIN_SOURCE = tailscale_$(ATHENA_TAILSCALE_BIN_VERSION)_riscv64.tgz
ATHENA_TAILSCALE_BIN_LICENSE = BSD-3-Clause

define ATHENA_TAILSCALE_BIN_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/tailscaled $(TARGET_DIR)/usr/sbin/tailscaled
	$(INSTALL) -D -m 0755 $(@D)/tailscale $(TARGET_DIR)/usr/bin/tailscale
endef

define ATHENA_TAILSCALE_BIN_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(ATHENA_TAILSCALE_BIN_PKGDIR)/S45tailscaled \
		$(TARGET_DIR)/etc/init.d/S45tailscaled
endef

$(eval $(generic-package))
