/* USB console (Serial/JTAG). */
#pragma once

#include "esp_console.h"

/* Register the board commands plus the app's (pda_console_register_app) and start the REPL. */
void pda_console_start(void);
void pda_console_register(const char *name, const char *help, esp_console_cmd_func_t fn);

/* Implemented by the app: feed, outbox, ui, ble commands. */
void pda_console_register_app(void);
