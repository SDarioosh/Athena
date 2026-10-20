# Every .c in main/ is part of the firmware; the pure modules are also built by test/ on
# the host. button_gesture.c is used only by the board layer, so the board component
# compiles it (main's archive is linked before board's and would not resolve it).
idf_component_register(
    SRC_DIRS "."
    EXCLUDE_SRCS "button_gesture.c"
    INCLUDE_DIRS "."
    REQUIRES board driver esp_driver_i2s esp_timer console fatfs nvs_flash esp_pm bt espcoredump spi_flash esp_partition
)
