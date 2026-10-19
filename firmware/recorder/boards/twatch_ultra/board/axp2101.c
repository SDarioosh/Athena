#include "axp2101.h"

#include "esp_check.h"
#include "esp_log.h"
#include "i2c_bus.h"

static const char *TAG = "axp2101";
static i2c_dev_t s_dev;

/* Registers */
#define REG_STATUS1 0x00        /* bit5 VBUS good, bit3 battery present */
#define REG_STATUS2 0x01        /* bits[7:5] 01 = charging, 10 = discharging */
#define REG_IC_TYPE 0x03        /* 0x4A */
#define REG_RESET_FUEL_GAUGE 0x17
#define REG_CHARGE_GAUGE_WDT_CTRL 0x18 /* bit2 backup (button) battery charge enable */
#define REG_IRQ_OFF_ON_LEVEL 0x27
#define REG_ADC_CHANNEL_CTRL 0x30
#define REG_ADC_VBAT_H 0x34
#define REG_INTEN1 0x40
#define REG_INTSTS1 0x48
#define REG_ICC_CHG_SET 0x62    /* bits[4:0] constant charge current */
#define REG_CV_CHG_VOL_SET 0x64 /* bits[2:0] charge target voltage */
#define REG_CHGLED_SET_CTRL 0x69
#define REG_BTN_BAT_CHG_VOL_SET 0x6A /* bits[2:0]: 2.6 V + n * 0.1 V */
#define REG_DC_ONOFF 0x80       /* bit0 DC1 .. bit4 DC5 */
#define REG_LDO_ONOFF0 0x90     /* bit0 ALDO1..bit3 ALDO4, bit4 BLDO1, bit5 BLDO2, bit6 CPUSLDO, bit7 DLDO1 */
#define REG_LDO_ONOFF1 0x91     /* bit0 DLDO2 */
#define REG_LDO_VOL_BASE 0x92   /* ALDO1 .. DLDO2 in rail order */
#define REG_BAT_PARAMS 0xA1
#define REG_FUEL_GAUGE_CTRL 0xA2
#define REG_BAT_PERCENT 0xA4

#define CHIP_ID 0x4A

static const char *const rail_names[AXP_RAIL_COUNT] = {
    "DC1", "DC2", "DC3", "DC4", "DC5", "ALDO1", "ALDO2", "ALDO3", "ALDO4",
    "BLDO1", "BLDO2", "CPUSLDO", "DLDO1", "DLDO2",
};

const char *axp2101_rail_name(axp_rail_t rail)
{
    return rail < AXP_RAIL_COUNT ? rail_names[rail] : "?";
}

static bool rail_bit(axp_rail_t rail, uint8_t *reg, uint8_t *bit)
{
    if (rail <= AXP_DC5) {
        *reg = REG_DC_ONOFF;
        *bit = (uint8_t)rail;
    } else if (rail <= AXP_DLDO1) {
        *reg = REG_LDO_ONOFF0;
        *bit = (uint8_t)(rail - AXP_ALDO1);
    } else if (rail == AXP_DLDO2) {
        *reg = REG_LDO_ONOFF1;
        *bit = 0;
    } else {
        return false;
    }
    return true;
}

esp_err_t axp2101_init(uint8_t i2c_addr)
{
    ESP_RETURN_ON_ERROR(i2c_bus_add(i2c_addr, &s_dev), TAG, "add");
    uint8_t id = 0;
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, REG_IC_TYPE, &id), TAG, "read id");
    if (id != CHIP_ID) {
        ESP_LOGW(TAG, "unexpected IC type 0x%02X (want 0x%02X)", id, CHIP_ID);
    }
    return ESP_OK;
}

esp_err_t axp2101_rail_enable(axp_rail_t rail, bool on)
{
    uint8_t reg, bit;
    ESP_RETURN_ON_FALSE(rail_bit(rail, &reg, &bit), ESP_ERR_INVALID_ARG, TAG, "rail");
    return i2c_reg_update(&s_dev, reg, (uint8_t)(1u << bit), on ? (uint8_t)(1u << bit) : 0);
}

bool axp2101_rail_is_enabled(axp_rail_t rail)
{
    uint8_t reg, bit, val = 0;
    if (!rail_bit(rail, &reg, &bit) || i2c_reg_read8(&s_dev, reg, &val) != ESP_OK) {
        return false;
    }
    return (val >> bit) & 1u;
}

static bool ldo_vol_reg(axp_rail_t rail, uint8_t *reg)
{
    if (rail < AXP_ALDO1 || rail > AXP_DLDO1 || rail == AXP_CPUSLDO) {
        return false;
    }
    *reg = (uint8_t)(REG_LDO_VOL_BASE + (rail - AXP_ALDO1));
    return true;
}

esp_err_t axp2101_rail_set_mv(axp_rail_t rail, int mv)
{
    uint8_t reg;
    ESP_RETURN_ON_FALSE(ldo_vol_reg(rail, &reg), ESP_ERR_INVALID_ARG, TAG, "ldo");
    ESP_RETURN_ON_FALSE(mv >= 500 && mv <= 3500 && mv % 100 == 0, ESP_ERR_INVALID_ARG, TAG, "mv");
    return i2c_reg_update(&s_dev, reg, 0x1F, (uint8_t)((mv - 500) / 100));
}

int axp2101_rail_get_mv(axp_rail_t rail)
{
    uint8_t reg, val = 0;
    if (!ldo_vol_reg(rail, &reg) || i2c_reg_read8(&s_dev, reg, &val) != ESP_OK) {
        return -1;
    }
    return 500 + (val & 0x1F) * 100;
}

/* ICC_CHG_SET: 0..8 -> 0..200 mA in 25 mA steps, 9..16 -> 300..1000 mA in 100 mA steps. */
static int icc_to_ma(uint8_t v)
{
    return v <= 8 ? v * 25 : (v <= 16 ? 300 + (v - 9) * 100 : -1);
}

esp_err_t axp2101_set_charge_current_ma(int ma)
{
    uint8_t best = 0;
    for (uint8_t v = 0; v <= 16; v++) {
        if (icc_to_ma(v) <= ma) {
            best = v;
        }
    }
    return i2c_reg_update(&s_dev, REG_ICC_CHG_SET, 0x1F, best);
}

int axp2101_get_charge_current_ma(void)
{
    uint8_t v = 0;
    if (i2c_reg_read8(&s_dev, REG_ICC_CHG_SET, &v) != ESP_OK) {
        return -1;
    }
    return icc_to_ma(v & 0x1F);
}

esp_err_t axp2101_set_charge_voltage_4v2(void)
{
    /* bits[2:0]: 1 = 4.0 V, 2 = 4.1 V, 3 = 4.2 V, 4 = 4.35 V, 5 = 4.4 V */
    return i2c_reg_update(&s_dev, REG_CV_CHG_VOL_SET, 0x07, 3);
}

esp_err_t axp2101_chgled_off(void)
{
    /* Manual LED control (bits[2:1] = 10, bit0 = 1), manual state off (bits[5:4] = 00).
     * Same register value XPowersLib uses for "LED off". */
    return i2c_reg_update(&s_dev, REG_CHGLED_SET_CTRL, 0x37, 0x05);
}

esp_err_t axp2101_backup_charge_enable(int mv)
{
    ESP_RETURN_ON_FALSE(mv >= 2600 && mv <= 3300 && mv % 100 == 0, ESP_ERR_INVALID_ARG, TAG, "mv");
    ESP_RETURN_ON_ERROR(i2c_reg_update(&s_dev, REG_BTN_BAT_CHG_VOL_SET, 0x07, (uint8_t)((mv - 2600) / 100)),
                        TAG, "vol");
    return i2c_reg_update(&s_dev, REG_CHARGE_GAUGE_WDT_CTRL, 0x04, 0x04);
}

esp_err_t axp2101_adc_enable(uint8_t channel_mask)
{
    return i2c_reg_write8(&s_dev, REG_ADC_CHANNEL_CTRL, channel_mask);
}

esp_err_t axp2101_read_battery(axp_battery_t *out)
{
    uint8_t st1, st2, vb[2], pct;
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, REG_STATUS1, &st1), TAG, "status1");
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, REG_STATUS2, &st2), TAG, "status2");
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, REG_ADC_VBAT_H, vb, 2), TAG, "vbat");
    ESP_RETURN_ON_ERROR(i2c_reg_read8(&s_dev, REG_BAT_PERCENT, &pct), TAG, "percent");
    out->battery_present = (st1 >> 3) & 1u;
    out->vbus_good = (st1 >> 5) & 1u;
    out->charging = ((st2 >> 5) & 0x07) == 0x01;
    out->millivolts = ((vb[0] & 0x3F) << 8) | vb[1]; /* 1 mV / LSB */
    out->percent = out->battery_present ? (pct & 0x7F) : -1;
    return ESP_OK;
}

esp_err_t axp2101_irq_enable(uint32_t mask)
{
    uint8_t en[3] = {(uint8_t)mask, (uint8_t)(mask >> 8), (uint8_t)(mask >> 16)};
    return i2c_reg_write(&s_dev, REG_INTEN1, en, 3);
}

esp_err_t axp2101_irq_read_clear(uint32_t *status)
{
    uint8_t st[3];
    ESP_RETURN_ON_ERROR(i2c_reg_read(&s_dev, REG_INTSTS1, st, 3), TAG, "intsts");
    *status = (uint32_t)st[0] | ((uint32_t)st[1] << 8) | ((uint32_t)st[2] << 16);
    if (*status) {
        /* write 1 to clear */
        ESP_RETURN_ON_ERROR(i2c_reg_write(&s_dev, REG_INTSTS1, st, 3), TAG, "clear");
    }
    return ESP_OK;
}

esp_err_t axp2101_gauge_write_params(const uint8_t *params, size_t len)
{
    ESP_RETURN_ON_FALSE(params && len == 128, ESP_ERR_INVALID_ARG, TAG, "128 bytes");
    esp_err_t err = ESP_OK;
    uint8_t readback[128] = {0};
    i2c_bus_lock();
    /* Sequence as in XPowersLib writeGaugeData()/compareGaugeData(). */
    err = err ?: i2c_reg_update(&s_dev, REG_RESET_FUEL_GAUGE, 0x04, 0x04);
    err = err ?: i2c_reg_update(&s_dev, REG_RESET_FUEL_GAUGE, 0x04, 0x00);
    err = err ?: i2c_reg_update(&s_dev, REG_FUEL_GAUGE_CTRL, 0x01, 0x00);
    err = err ?: i2c_reg_update(&s_dev, REG_FUEL_GAUGE_CTRL, 0x01, 0x01);
    for (size_t i = 0; i < 128 && err == ESP_OK; i++) {
        err = i2c_reg_write8(&s_dev, REG_BAT_PARAMS, params[i]);
    }
    err = err ?: i2c_reg_update(&s_dev, REG_FUEL_GAUGE_CTRL, 0x01, 0x00);
    err = err ?: i2c_reg_update(&s_dev, REG_FUEL_GAUGE_CTRL, 0x01, 0x01);
    for (size_t i = 0; i < 128 && err == ESP_OK; i++) {
        err = i2c_reg_read8(&s_dev, REG_BAT_PARAMS, &readback[i]);
    }
    err = err ?: i2c_reg_update(&s_dev, REG_FUEL_GAUGE_CTRL, 0x01, 0x00);
    err = err ?: i2c_reg_update(&s_dev, REG_FUEL_GAUGE_CTRL, 0x10, 0x10);
    err = err ?: i2c_reg_update(&s_dev, REG_RESET_FUEL_GAUGE, 0x04, 0x04);
    err = err ?: i2c_reg_update(&s_dev, REG_RESET_FUEL_GAUGE, 0x04, 0x00);
    i2c_bus_unlock();
    ESP_RETURN_ON_ERROR(err, TAG, "gauge write");
    for (size_t i = 0; i < 128; i++) {
        if (readback[i] != params[i]) {
            ESP_LOGE(TAG, "gauge params read-back differs at byte %u (0x%02X != 0x%02X)",
                     (unsigned)i, readback[i], params[i]);
            return ESP_ERR_INVALID_RESPONSE;
        }
    }
    return ESP_OK;
}

void axp2101_log_registers(void)
{
    uint8_t dc = 0, ldo0 = 0, ldo1 = 0, icc = 0, cv = 0, led = 0, adc = 0, key = 0, en[3] = {0};
    uint8_t bkp = 0, bkpv = 0;
    i2c_reg_read8(&s_dev, REG_CHARGE_GAUGE_WDT_CTRL, &bkp);
    i2c_reg_read8(&s_dev, REG_BTN_BAT_CHG_VOL_SET, &bkpv);
    i2c_reg_read8(&s_dev, REG_DC_ONOFF, &dc);
    i2c_reg_read8(&s_dev, REG_LDO_ONOFF0, &ldo0);
    i2c_reg_read8(&s_dev, REG_LDO_ONOFF1, &ldo1);
    i2c_reg_read8(&s_dev, REG_ICC_CHG_SET, &icc);
    i2c_reg_read8(&s_dev, REG_CV_CHG_VOL_SET, &cv);
    i2c_reg_read8(&s_dev, REG_CHGLED_SET_CTRL, &led);
    i2c_reg_read8(&s_dev, REG_ADC_CHANNEL_CTRL, &adc);
    i2c_reg_read8(&s_dev, REG_IRQ_OFF_ON_LEVEL, &key);
    i2c_reg_read(&s_dev, REG_INTEN1, en, 3);
    ESP_LOGI(TAG, "reg 0x80 DC_ONOFF=0x%02X  0x90 LDO_ONOFF0=0x%02X  0x91 LDO_ONOFF1=0x%02X", dc,
             ldo0, ldo1);
    for (axp_rail_t r = AXP_DC1; r < AXP_RAIL_COUNT; r++) {
        const int mv = axp2101_rail_get_mv(r);
        if (mv >= 0) {
            ESP_LOGI(TAG, "  %-7s %s  %d mV", rail_names[r],
                     axp2101_rail_is_enabled(r) ? "ON " : "off", mv);
        } else {
            ESP_LOGI(TAG, "  %-7s %s", rail_names[r], axp2101_rail_is_enabled(r) ? "ON " : "off");
        }
    }
    ESP_LOGI(TAG, "reg 0x62 ICC=0x%02X (%d mA)  0x64 CV=0x%02X  0x69 CHGLED=0x%02X  0x30 ADC=0x%02X",
             icc, icc_to_ma(icc & 0x1F), cv, led, adc);
    ESP_LOGI(TAG, "reg 0x18=0x%02X (bit2 RTC backup charge %s)  0x6A backup target %d mV", bkp,
             (bkp & 0x04) ? "on" : "off", 2600 + (bkpv & 0x07) * 100);
    ESP_LOGI(TAG, "reg 0x27 key timing=0x%02X (bits[1:0] power-off hold: %d s)  INTEN=%02X %02X %02X",
             key, 4 + 2 * (key & 0x03), en[0], en[1], en[2]);
}
