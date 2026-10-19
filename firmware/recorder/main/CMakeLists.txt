idf_component_register(
    SRCS
        "app_main.c"
        "audio_dsp.c"
        "audio_stats.c"
        "console_cmds.c"
        "font5x7.c"
        "isotime.c"
        "mic.c"
        "recorder.c"
        "textfb.c"
        "ui.c"
        "wav.c"
    INCLUDE_DIRS "."
    REQUIRES board driver esp_driver_i2s esp_timer console fatfs nvs_flash esp_pm
)
