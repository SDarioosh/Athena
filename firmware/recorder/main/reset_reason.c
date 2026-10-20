#include "reset_reason.h"

#include "esp_system.h"

const char *reset_reason_name(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:
        return "poweron"; /* also the PWR key turning the watch on */
    case ESP_RST_EXT:
        return "ext";
    case ESP_RST_SW:
        return "sw"; /* esp_restart(): console reboot, after a flash */
    case ESP_RST_PANIC:
        return "panic"; /* a crash; `coredump` shows where */
    case ESP_RST_INT_WDT:
        return "int_wdt";
    case ESP_RST_TASK_WDT:
        return "task_wdt";
    case ESP_RST_WDT:
        return "wdt";
    case ESP_RST_DEEPSLEEP:
        return "deepsleep";
    case ESP_RST_BROWNOUT:
        return "brownout";
    case ESP_RST_SDIO:
        return "sdio";
    case ESP_RST_USB:
        return "usb"; /* USB Serial/JTAG reset: esptool, a serial monitor */
    case ESP_RST_JTAG:
        return "jtag";
    case ESP_RST_EFUSE:
        return "efuse";
    case ESP_RST_PWR_GLITCH:
        return "pwr_glitch";
    case ESP_RST_CPU_LOCKUP:
        return "cpu_lockup";
    default:
        return "unknown";
    }
}
