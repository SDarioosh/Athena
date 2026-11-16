/* The Brief tab's rules (T5 plan §4.1 `briefing`, §4.2 `digest_refresh`; the T5's
 * draw_briefing in ui_pda.c): the digest's headline and summary, its day, and the refresh
 * action with its pending and failed state. No LVGL; host-tested (test/test_brief.c). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "app_state.h"
#include "feed.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BRIEF_HEADLINE_MAX 240 /* bytes: a longer first paragraph is no headline */

/* The relay writes `text` as the headline, a blank line, then the summary. head gets the
 * headline ("" when the text has no blank line or the first paragraph is too long to be
 * one); *body points into text at the summary (the whole text without a headline). */
void brief_split(const char *text, char *head, size_t head_cap, const char **body);

/* The digest's day number, FEED_NO_DAY without a briefing or a readable date. */
int32_t brief_day(const feed_t *f);

/* "Today", "Yesterday", else "Mon 19 Oct". */
void brief_day_name(int32_t day, int32_t today, char *out, size_t cap);

/* The date a refresh asks for (T5): the briefing's, or today's without one. out: 11 bytes. */
void brief_refresh_date(const feed_t *f, int32_t today, char *out, size_t cap);

/* digest_refresh for that date into out (PDA_ACTION_BUF): its length, 0 on error. */
size_t brief_refresh_action(const feed_t *f, int32_t today, char *out);

/* The queued digest_refresh for date, or NULL. */
const app_pending_t *brief_find_pending(const app_pending_t *p, int n, const char *date);
/* The server refused a digest_refresh for date. */
bool brief_find_failed(const app_failed_t *fl, int n, const char *date);

#ifdef __cplusplus
}
#endif
