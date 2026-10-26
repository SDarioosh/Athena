#include "event_log.h"

#include <stdio.h>
#include <string.h>

#include "isotime.h"

/* AXP2101 interrupt bits (boards/common/axp2101.h), copied so this pure file needs no board
 * header: the key's two edges, and the PMU's own short/long press decision. */
#define PMU_KEY_POSITIVE (1u << 8)
#define PMU_KEY_NEGATIVE (1u << 9)

void el_ring_init(el_ring_t *r, uint16_t next)
{
    memset(r->e, 0, sizeof(r->e));
    r->next = next;
}

unsigned el_ring_add(el_ring_t *r, const el_entry_t *e)
{
    const unsigned slot = r->next % EVENT_LOG_CAP;
    const unsigned block = slot / EVENT_LOG_BLOCK;
    if (slot % EVENT_LOG_BLOCK == 0) {
        memset(&r->e[slot], 0, sizeof(el_entry_t) * EVENT_LOG_BLOCK);
    }
    r->e[slot] = *e;
    r->next++;
    return block;
}

const el_entry_t *el_ring_get(const el_ring_t *r, unsigned i)
{
    if (i >= EVENT_LOG_CAP) {
        return NULL;
    }
    /* The oldest entry is the one the next add overwrites. */
    const el_entry_t *e = &r->e[(r->next + i) % EVENT_LOG_CAP];
    return e->kind == EL_NONE ? NULL : e;
}

const char *el_reset_name(int why)
{
    static const char *const names[EL_RST_COUNT_] = {
        [EL_RST_UNKNOWN] = "unknown reset",
        [EL_RST_POWERON] = "power-on or reset",
        [EL_RST_EXT] = "external reset",
        [EL_RST_SW] = "software restart",
        [EL_RST_PANIC] = "crash (panic)",
        [EL_RST_INT_WDT] = "interrupt watchdog",
        [EL_RST_TASK_WDT] = "task watchdog",
        [EL_RST_WDT] = "watchdog",
        [EL_RST_DEEPSLEEP] = "out of standby",
        [EL_RST_BROWNOUT] = "brownout",
        [EL_RST_USB] = "USB reset",
        [EL_RST_JTAG] = "JTAG reset",
        [EL_RST_OTHER] = "other reset",
    };
    return why >= 0 && why < EL_RST_COUNT_ && names[why] ? names[why] : "?";
}

const char *el_button_name(int why)
{
    static const char *const names[EL_BTN_COUNT_] = {
        [EL_BTN_PWR_SHORT] = "PWR short",
        [EL_BTN_PWR_SHORT_IGNORED] = "PWR short ignored",
        [EL_BTN_PWR_LONG] = "PWR held",
        [EL_BTN_BOOT_SHORT] = "BOOT short",
        [EL_BTN_BOOT_DOUBLE] = "BOOT double",
        [EL_BTN_BOOT_LONG] = "BOOT held",
    };
    return why > 0 && why < EL_BTN_COUNT_ && names[why] ? names[why] : "?";
}

const char *el_trigger_name(int why)
{
    static const char *const names[EL_TRIG_COUNT_] = {
        [EL_TRIG_PWR] = "PWR",
        [EL_TRIG_PWR_LONG] = "PWR held",
        [EL_TRIG_CONSOLE] = "console",
        [EL_TRIG_RESUME] = "resume after a restart",
        [EL_TRIG_AFTER_NOTE] = "resume after a note",
        [EL_TRIG_NOTE] = "a note",
        [EL_TRIG_MUTE] = "mute",
        [EL_TRIG_BATT_LOW] = "low battery",
    };
    return why > 0 && why < EL_TRIG_COUNT_ && names[why] ? names[why] : "?";
}

const char *el_end_name(int why)
{
    static const char *const names[EL_END_COUNT_] = {
        [EL_END_STOPPED] = "stopped",
        [EL_END_CARD_FULL] = "card full",
        [EL_END_ERROR] = "error",
        [EL_END_MAX] = "length limit",
    };
    return why >= 0 && why < EL_END_COUNT_ && names[why] ? names[why] : "?";
}

static const char *rec_name(uint8_t aux)
{
    return aux == EL_REC_NOTE ? "note" : "conversation";
}

/* "PMU 0x000b10: key edges + -", or "no key edges" when the PMU reported a press without
 * seeing the key move. */
static void pmu_bits(int32_t arg, char *out, size_t cap)
{
    const uint32_t st = (uint32_t)arg;
    if (st == 0) {
        snprintf(out, cap, "no PMU bits");
        return;
    }
    const bool pos = st & PMU_KEY_POSITIVE, neg = st & PMU_KEY_NEGATIVE;
    snprintf(out, cap, "PMU 0x%06x: %s%s%s", (unsigned)st, pos || neg ? "key edges" : "no key edges",
             pos ? " +" : "", neg ? " -" : "");
}

void el_format(const el_entry_t *e, char *out, size_t cap)
{
    char when[24];
    if (e->t) {
        isotime_format(e->t, when, sizeof(when));
    } else {
        snprintf(when, sizeof(when), "(clock unknown)");
    }
    char what[96];
    char extra[48];
    switch (e->kind) {
    case EL_BOOT:
        snprintf(what, sizeof(what), "boot: %s%s", el_reset_name(e->why),
                 e->arg == 1 ? ", woken from standby"
                 : e->arg == 2 ? ", recording resumed" : "");
        break;
    case EL_BUTTON:
        if (e->why == EL_BTN_PWR_SHORT_IGNORED) {
            snprintf(what, sizeof(what), "button %s (%d ms after the previous one)",
                     el_button_name(e->why), (int)e->arg);
        } else if (e->why == EL_BTN_PWR_SHORT || e->why == EL_BTN_PWR_LONG) {
            pmu_bits(e->arg, extra, sizeof(extra));
            snprintf(what, sizeof(what), "button %s (%s)", el_button_name(e->why), extra);
        } else {
            snprintf(what, sizeof(what), "button %s", el_button_name(e->why));
        }
        break;
    case EL_START:
        snprintf(what, sizeof(what), "%s started by %s", rec_name(e->aux), el_trigger_name(e->why));
        break;
    case EL_STOP:
        snprintf(what, sizeof(what), "%s stop asked by %s", rec_name(e->aux),
                 el_trigger_name(e->why));
        break;
    case EL_END:
        snprintf(what, sizeof(what), "%s ended (%s) after %d s", rec_name(e->aux),
                 el_end_name(e->why), (int)e->arg);
        break;
    case EL_MUTE:
        snprintf(what, sizeof(what), "%s", e->why ? "muted" : "unmuted");
        break;
    case EL_BATT_LOW:
        snprintf(what, sizeof(what), "battery %s (%d mV)",
                 e->why ? "low: recordings refused" : "OK again", (int)e->arg);
        break;
    case EL_CARD_RESET:
        snprintf(what, sizeof(what), "card powered off and on after write error 0x%x: %s",
                 (unsigned)e->arg, e->why ? "mounted again" : "still failing");
        break;
    default:
        snprintf(what, sizeof(what), "entry kind %d", (int)e->kind);
        break;
    }
    char batt[16] = "";
    if (e->batt >= 0) {
        snprintf(batt, sizeof(batt), "; batt %d%%", (int)e->batt);
    }
    snprintf(out, cap, "%s +%lus  %s%s", when, (unsigned long)e->up_s, what, batt);
}
