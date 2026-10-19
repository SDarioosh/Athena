#include "sdcard.h"

#include "driver/gpio.h"
#include "driver/sdspi_host.h"
#include "driver/spi_common.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "pins.h"
#include "sdmmc_cmd.h"

static const char *TAG = "sdcard";
static sdmmc_card_t *s_card;
static bool s_bus_up;

#define SD_SPI_HOST SPI3_HOST
/* LilyGoLib uses 4 MHz on this shared bus; raise once the bus leakage question (plan §2.2)
 * is settled and the card is known to be good at 20 MHz. */
#define SD_SPI_FREQ_KHZ 4000

static void release_bus_pins(void)
{
    const int pins[] = {PIN_SPI_MOSI, PIN_SPI_MISO, PIN_SPI_SCK, PIN_SD_CS};
    for (unsigned i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
        gpio_reset_pin(pins[i]);
        gpio_set_direction(pins[i], GPIO_MODE_INPUT);
        gpio_set_pull_mode(pins[i], GPIO_FLOATING);
    }
}

esp_err_t sdcard_mount(void)
{
    if (s_card) {
        return ESP_OK;
    }
    const spi_bus_config_t bus = {
        .mosi_io_num = PIN_SPI_MOSI,
        .miso_io_num = PIN_SPI_MISO,
        .sclk_io_num = PIN_SPI_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4096,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(SD_SPI_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "spi bus");
    s_bus_up = true;

    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    host.slot = SD_SPI_HOST;
    host.max_freq_khz = SD_SPI_FREQ_KHZ;
    sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot.gpio_cs = PIN_SD_CS;
    slot.host_id = SD_SPI_HOST;

    const esp_vfs_fat_sdmmc_mount_config_t mount = {
        .format_if_mount_failed = false,
        /* recording chunk + BLE read + a .met rewrite + console `get`, with headroom */
        .max_files = 6,
        .allocation_unit_size = 16 * 1024,
    };
    esp_err_t err = esp_vfs_fat_sdspi_mount(SDCARD_MOUNT_POINT, &host, &slot, &mount, &s_card);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "mount failed: %s", esp_err_to_name(err));
        spi_bus_free(SD_SPI_HOST);
        s_bus_up = false;
        release_bus_pins();
        s_card = NULL;
        return err;
    }
    ESP_LOGI(TAG, "mounted %s: %s, %llu MB", SDCARD_MOUNT_POINT, s_card->cid.name,
             ((uint64_t)s_card->csd.capacity * s_card->csd.sector_size) / (1024 * 1024));
    return ESP_OK;
}

esp_err_t sdcard_unmount(void)
{
    if (s_card) {
        esp_vfs_fat_sdcard_unmount(SDCARD_MOUNT_POINT, s_card);
        s_card = NULL;
    }
    if (s_bus_up) {
        spi_bus_free(SD_SPI_HOST);
        s_bus_up = false;
    }
    release_bus_pins();
    return ESP_OK;
}

bool sdcard_is_mounted(void)
{
    return s_card != NULL;
}

esp_err_t sdcard_info(uint64_t *total_bytes, uint64_t *free_bytes)
{
    ESP_RETURN_ON_FALSE(s_card, ESP_ERR_INVALID_STATE, TAG, "not mounted");
    return esp_vfs_fat_info(SDCARD_MOUNT_POINT, total_bytes, free_bytes);
}
