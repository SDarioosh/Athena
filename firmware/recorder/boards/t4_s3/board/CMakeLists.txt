# Board layer for the LilyGO T4-S3 conference recorder. Implements main/board.h.
idf_component_register(
    SRCS
        "board_t4_s3.c"
        "display.c"
        "sy6970.c"
        "sy6970_codec.c"
        "../../../main/click_counter.c" # pure click counter, host-tested in test/
        "../../../main/key_press.c"     # pure press detector, host-tested in test/
        # Shared with the T-Watch boards (boards/common/).
        "../../common/flashfs.c"
        "../../common/i2c_bus.c"
    INCLUDE_DIRS "." "../../common"
    # board.h (the interface this component implements) and isotime.h live in main/.
    PRIV_INCLUDE_DIRS "../../../main"
    REQUIRES esp_driver_gpio esp_driver_i2c esp_driver_spi esp_lcd fatfs wear_levelling nvs_flash esp_timer
    PRIV_REQUIRES esp_lcd_co5300
)
