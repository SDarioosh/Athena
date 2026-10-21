# Board layer for the LilyGO T-Watch S3. Implements main/board.h.
idf_component_register(
    SRCS
        "board_twatch_s3.c"
        "display.c"
        "flashfs.c"
        "pcf8563.c"
        "pcf8563_codec.c"
        "../../../main/click_counter.c" # pure click counter, host-tested in test/
        # Chip drivers shared with the T-Watch Ultra (boards/common/).
        "../../common/axp2101.c"
        "../../common/drv2605.c"
        "../../common/i2c_bus.c"
    INCLUDE_DIRS "." "../../common"
    # board.h (the interface this component implements) and isotime.h live in main/.
    PRIV_INCLUDE_DIRS "../../../main"
    REQUIRES esp_driver_gpio esp_driver_i2c esp_driver_spi esp_lcd fatfs wear_levelling nvs_flash esp_timer
)
