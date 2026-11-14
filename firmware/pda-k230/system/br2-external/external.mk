################################################################################
#
# Athena PDA br2-external tree (firmware/pda-k230/system/br2-external)
#
################################################################################

# firmware/ in the Athena repository: the local packages build from here, so the
# UI can compile firmware/pda/main and firmware/recorder/main where they are and
# syncd can install firmware/recorder/tools/athena_relay (docs/k230-pda-plan.md,
# "Reused unchanged").
ATHENA_PDA_FW_DIR = $(realpath $(BR2_EXTERNAL_ATHENA_PDA_PATH)/../../..)

# rsync filters shared by the local packages: never copy host build trees,
# virtualenvs or caches into the Buildroot build directory.
ATHENA_PDA_RSYNC_JUNK = \
	--exclude=build/ --exclude=build-*/ --exclude=.venv/ \
	--exclude=__pycache__/ --exclude=*.pyc --exclude=.pytest_cache/

include $(sort $(wildcard $(BR2_EXTERNAL_ATHENA_PDA_PATH)/package/*/*.mk))
