# Every .c in main/ is part of the firmware; the pure modules are also built by test/ on
# the host.
idf_component_register(
    SRC_DIRS "."
    INCLUDE_DIRS "."
    REQUIRES board driver esp_driver_i2s esp_timer console fatfs nvs_flash esp_pm bt
)
