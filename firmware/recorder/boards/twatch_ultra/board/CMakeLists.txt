# Board layer for the LilyGO T-Watch Ultra. Implements main/board.h.
idf_component_register(
    SRCS
        "axp2101.c"
        "board_twatch_ultra.c"
        "display.c"
        "drv2605.c"
        "i2c_bus.c"
        "pcf85063a.c"
        "pcf85063a_codec.c"
        "sdcard.c"
        "xl9555.c"
        "../../../main/button_gesture.c" # pure gesture machine, host-tested in test/
    INCLUDE_DIRS "."
    # board.h (the interface this component implements) and isotime.h live in main/.
    PRIV_INCLUDE_DIRS "../../../main"
    REQUIRES esp_driver_gpio esp_driver_i2c esp_driver_spi esp_driver_sdspi esp_lcd fatfs sdmmc nvs_flash esp_timer
    PRIV_REQUIRES esp_lcd_co5300
)
