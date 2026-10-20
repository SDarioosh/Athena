/* The e-paper panel (ED047TC1, 960x540, 16 greys) through epdiy board "v7"
 * (docs/t5-pda-plan.md §1, §5 refresh policy).
 *
 * Draw into pda_epd_fb() with epdiy's drawing and font functions, then call
 * pda_epd_update(). The refresh policy lives here: a partial (non-flashing, GL16) update
 * of the changed area, or a full flashing (GC16) one when the caller asks, and after every
 * PDA_EPD_PARTIAL_LIMIT partials, to clear ghosting. */
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
    PDA_EPD_PARTIAL, /* changed area, no flash (full if the partial budget ran out) */
    PDA_EPD_FULL,    /* whole screen, flashing: clears ghosting */
} pda_epd_kind_t;

typedef struct {
    uint32_t full, partial;
    int last_full_ms, last_partial_ms; /* duration of the last update of each kind */
    int partials_since_full;
} pda_epd_stats_t;

/* epdiy on the board's I2C bus (TPS65185 and PCA9535 port 1 belong to epdiy after this). */
esp_err_t pda_epd_init(i2c_master_bus_handle_t bus);

uint8_t *pda_epd_fb(void);
/* Fill the framebuffer white. */
void pda_epd_clear_fb(void);

/* Push the framebuffer to the panel. Blocks for the refresh (~0.3-1.5 s). Returns the
 * kind actually done. */
pda_epd_kind_t pda_epd_update(pda_epd_kind_t kind);
/* Partial update of one area only (pending marks, a tapped row). */
pda_epd_kind_t pda_epd_update_area(EpdRect area);

void pda_epd_get_stats(pda_epd_stats_t *out);

#ifdef __cplusplus
}
#endif
