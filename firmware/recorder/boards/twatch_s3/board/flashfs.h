/* The T-Watch S3 has no SD card: storage is a FAT file system (with wear levelling) in the
 * "storage" partition of its 16 MB flash (partitions_twatch_s3.csv). */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define FLASHFS_MOUNT_POINT "/flash"
#define FLASHFS_PARTITION "storage"

/* Formats the partition if it holds no file system yet (first boot after flashing). */
esp_err_t flashfs_mount(void);
esp_err_t flashfs_unmount(void);
bool flashfs_is_mounted(void);
esp_err_t flashfs_info(uint64_t *total_bytes, uint64_t *free_bytes);
