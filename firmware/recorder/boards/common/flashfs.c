#include "flashfs.h"

#include "esp_check.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "wear_levelling.h"

static const char *TAG = "flashfs";
static wl_handle_t s_wl = WL_INVALID_HANDLE;

esp_err_t flashfs_mount(void)
{
    if (s_wl != WL_INVALID_HANDLE) {
        return ESP_OK;
    }
    const esp_vfs_fat_mount_config_t mount = {
        /* Blank (or the factory firmware's leftovers) on the first boot after flashing. */
        .format_if_mount_failed = true,
        /* recording chunk + BLE read + a .met rewrite + console `get`, with headroom */
        .max_files = 6,
        .allocation_unit_size = CONFIG_WL_SECTOR_SIZE,
    };
    const esp_err_t err = esp_vfs_fat_spiflash_mount_rw_wl(FLASHFS_MOUNT_POINT, FLASHFS_PARTITION,
                                                           &mount, &s_wl);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "mount failed: %s", esp_err_to_name(err));
        s_wl = WL_INVALID_HANDLE;
        return err;
    }
    return ESP_OK;
}

esp_err_t flashfs_unmount(void)
{
    if (s_wl == WL_INVALID_HANDLE) {
        return ESP_OK;
    }
    const esp_err_t err = esp_vfs_fat_spiflash_unmount_rw_wl(FLASHFS_MOUNT_POINT, s_wl);
    s_wl = WL_INVALID_HANDLE;
    return err;
}

bool flashfs_is_mounted(void)
{
    return s_wl != WL_INVALID_HANDLE;
}

esp_err_t flashfs_info(uint64_t *total_bytes, uint64_t *free_bytes)
{
    ESP_RETURN_ON_FALSE(s_wl != WL_INVALID_HANDLE, ESP_ERR_INVALID_STATE, TAG, "not mounted");
    return esp_vfs_fat_info(FLASHFS_MOUNT_POINT, total_bytes, free_bytes);
}
