/* The e-paper panel (ED047TC1, 960x540, 16 greys) through epdiy board "v7"
 * (docs/t5-pda-plan.md §1, §5 refresh policy).
 *
 * Draw into pda_epd_fb() with epdiy's drawing and font functions, then call
 * pda_epd_update(). The refresh policy lives here: a partial (non-flashing, GL16) update
 * of the changed area, a flashing (GC16) one for a new screen, and a clean one (flash
 * white, redraw every pixel) when asked and after every PDA_EPD_PARTIAL_LIMIT updates, to
 * clear ghosting. epdiy only drives pixels that changed, so only a clean update touches
 * the rest of the panel. Measured times are in docs/t5-pda-plan.md §1. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "epdiy.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define PDA_EPD_W 960
#define PDA_EPD_H 540
#define PDA_EPD_PARTIAL_LIMIT 10
/* VCOM in mV: LilyGO's factory firmware default for this panel. */
#define PDA_EPD_VCOM_MV 1560

/* epdiy colours: 0x00 black .. 0xFF white (only the upper nibble counts). */
#define EPD_BLACK 0x00
#define EPD_DARK 0x50
#define EPD_GREY 0x90
#define EPD_LIGHT 0xD0
#define EPD_WHITE 0xFF

typedef enum {
    PDA_EPD_PARTIAL, /* changed pixels, GL16, no flash */
    PDA_EPD_FULL,    /* changed pixels, GC16, flashing: a new screen */
    PDA_EPD_CLEAN,   /* flash the panel white, then GC16 every pixel: clears ghosting */
} pda_epd_kind_t;

typedef struct {
    uint32_t count[3];   /* per pda_epd_kind_t */
    int last_ms[3];      /* duration of the last update of each kind */
    int since_clean;     /* partial and full updates since the last clean one */
} pda_epd_stats_t;

/* epdiy on the board's I2C bus (TPS65185 and PCA9535 port 1 belong to epdiy after this). */
esp_err_t pda_epd_init(i2c_master_bus_handle_t bus);

uint8_t *pda_epd_fb(void);
/* Fill the framebuffer white. */
void pda_epd_clear_fb(void);

/* Push the framebuffer to the panel. Blocks for the refresh (~0.1-1.6 s). Returns the
 * kind actually done (a partial or full update becomes clean when the budget ran out). */
pda_epd_kind_t pda_epd_update(pda_epd_kind_t kind);
/* Partial update of one area only (pending marks, a tapped row). */
pda_epd_kind_t pda_epd_update_area(EpdRect area);
/* A small partial update that doesn't count towards the clean budget (the minute clock). */
void pda_epd_update_light(EpdRect area);

void pda_epd_get_stats(pda_epd_stats_t *out);

#ifdef __cplusplus
}
#endif
