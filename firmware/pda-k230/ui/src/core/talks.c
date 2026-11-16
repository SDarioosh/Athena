#include "talks.h"

#include <stdio.h>
#include <string.h>

#include "app_state.h"
#include "pda_actions.h"
#include "pda_time.h"

void talks_rel_time(int64_t at, int64_t now, int tz_min, char *out, size_t cap)
{
    if (cap == 0) {
        return;
    }
    out[0] = 0;
    if (at <= 0) {
        return;
    }
    const int64_t ago = now - at;
    if (ago >= 0 && ago < 60) {
        snprintf(out, cap, "Just now");
        return;
    }
    if (ago >= 60 && ago < 3600) {
        snprintf(out, cap, "%d min ago", (int)(ago / 60));
        return;
    }
    if (ago >= 3600 && ago < 6 * 3600) {
        snprintf(out, cap, "%d h ago", (int)(ago / 3600));
        return;
    }
    const int32_t day = pda_local_day(at, tz_min), today = pda_local_day(now, tz_min);
    char hm[12], d[24];
    pda_fmt_hm(at, tz_min, hm, sizeof(hm));
    pda_fmt_day(day, d, sizeof(d));
    if (day == today) {
        snprintf(out, cap, "Today %s", hm);
    } else if (day == today - 1) {
        snprintf(out, cap, "Yesterday %s", hm);
    } else if (day == today + 1) {
        snprintf(out, cap, "Tomorrow %s", hm);
    } else if (day < today && day > today - 7) {
        /* "Fri 16 Oct" -> "Fri": the weekday alone is enough within a week */
        char wd[4];
        snprintf(wd, sizeof(wd), "%.3s", d);
        snprintf(out, cap, "%s %s", wd, hm);
    } else {
        snprintf(out, cap, "%s", d);
    }
}

void talks_people(const char *const *people, int n, int show, char *out, size_t cap)
{
    if (cap == 0) {
        return;
    }
    out[0] = 0;
    if (n <= 0) {
        return;
    }
    if (show < 1) {
        show = 1;
    }
    const int named = n <= show ? n : show;
    size_t m = 0;
    for (int i = 0; i < named && m < cap; i++) {
        const char *sep = i == 0 ? "" : (i == named - 1 && named == n) ? " and " : ", ";
        m += (size_t)snprintf(out + m, cap - m, "%s%s", sep, people[i] ? people[i] : "?");
    }
    if (named < n && m < cap) {
        snprintf(out + m, cap - m, " and %d more", n - named);
    }
}

/* the bytes of the UTF-8 code point at s */
static int cp_len(const char *s)
{
    const unsigned char c = (unsigned char)s[0];
    const int n = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 1;
    for (int i = 1; i < n; i++) {
        if (((unsigned char)s[i] >> 6) != 2) {
            return 1;
        }
    }
    return n;
}

void talks_initials(const char *name, char *out, size_t cap)
{
    if (cap == 0) {
        return;
    }
    size_t m = 0;
    int got = 0;
    bool word_start = true;
    for (const char *s = name ? name : ""; *s && got < 2;) {
        const int n = cp_len(s);
        const bool space = *s == ' ' || *s == '-' || *s == '\t';
        if (word_start && !space && ((unsigned char)*s >= 0x80 || (*s >= 'A' && *s <= 'Z') ||
                                     (*s >= 'a' && *s <= 'z') || (*s >= '0' && *s <= '9'))) {
            if (m + (size_t)n + 1 > cap) {
                break;
            }
            for (int i = 0; i < n; i++) {
                char c = s[i];
                if (n == 1 && c >= 'a' && c <= 'z') {
                    c = (char)(c - 'a' + 'A');
                }
                out[m++] = c;
            }
            got++;
        }
        word_start = space;
        s += n;
    }
    if (got == 0 && cap >= 2) {
        out[m++] = '?';
    }
    out[m < cap ? m : cap - 1] = 0;
}

int talks_conv_actions(const feed_t *f, const char *conv_id, int *idx, int max)
{
    int n = 0;
    for (int i = 0; f && conv_id && i < f->n_actions && n < max; i++) {
        if (f->actions[i].conv && strcmp(f->actions[i].conv, conv_id) == 0) {
            idx[n++] = i;
        }
    }
    return n;
}

void talks_item_view(const feed_action_t *a, talks_item_view_t *out)
{
    *out = (talks_item_view_t){0};
    const app_pending_t *p = app_pending_item(a->id);
    if (p) {
        out->pending = true;
        out->listed = p->listed;
        out->state = strcmp(p->info.status, "done") == 0        ? TALKS_ITEM_DONE
                     : strcmp(p->info.status, "dismissed") == 0 ? TALKS_ITEM_DISMISSED
                                                                : TALKS_ITEM_OPEN;
    }
    out->failed = app_failed_item(a->id) != NULL;
}

static talks_tap_t queue_status(int64_t id, const char *status, talks_tap_t ok)
{
    char json[PDA_ACTION_BUF];
    const size_t n = pda_action_item_status(json, id, status);
    const int rc = n ? app_queue(json, n) : -2;
    return rc == 0 ? ok : rc == -1 ? TALKS_TAP_FULL : TALKS_TAP_ERROR;
}

talks_tap_t talks_item_tap(int idx, bool long_press)
{
    const feed_t *f = app_feed();
    if (!f || idx < 0 || idx >= f->n_actions) {
        return TALKS_TAP_ERROR;
    }
    const int64_t id = f->actions[idx].id;
    const app_pending_t *p = app_pending_item(id);
    if (p) {
        if (!p->listed && app_cancel(p->id) == 0) {
            return TALKS_TAP_UNDONE;
        }
        return queue_status(id, "open", TALKS_TAP_REOPEN);
    }
    return queue_status(id, long_press ? "dismissed" : "done", TALKS_TAP_QUEUED);
}

int talks_action_index(int64_t id)
{
    const feed_t *f = app_feed();
    for (int i = 0; f && i < f->n_actions; i++) {
        if (f->actions[i].id == id) {
            return i;
        }
    }
    return -1;
}

talks_tap_t talks_item_tap_id(int64_t id, bool long_press)
{
    const int i = talks_action_index(id);
    return i < 0 ? TALKS_TAP_GONE : talks_item_tap(i, long_press);
}

void talks_item_line(const feed_action_t *a, const talks_item_view_t *v, int32_t today, char *out,
                     size_t cap)
{
    if (cap == 0) {
        return;
    }
    out[0] = 0;
    if (v->failed && !v->pending) {
        snprintf(out, cap, "Couldn't sync \xe2\x80\x93 the hub refused it");
        return;
    }
    if (v->pending) {
        const char *what = v->state == TALKS_ITEM_DONE        ? "Done"
                           : v->state == TALKS_ITEM_DISMISSED ? "Dismissed"
                                                              : "Reopened";
        snprintf(out, cap, "%s \xc2\xb7 %s", what, v->listed ? "sent, tap to reopen" : "tap to undo");
        return;
    }
    char due[48] = "";
    if (a->due_day != FEED_NO_DAY) {
        char d[24];
        pda_fmt_day(a->due_day, d, sizeof(d));
        if (a->due_day < today) {
            snprintf(due, sizeof(due), "Overdue since %s", d);
        } else if (a->due_day == today) {
            snprintf(due, sizeof(due), "Due today");
        } else if (a->due_day == today + 1) {
            snprintf(due, sizeof(due), "Due tomorrow");
        } else {
            snprintf(due, sizeof(due), "Due %s", d);
        }
    }
    snprintf(out, cap, "%s%s%s", due, due[0] && a->owner ? " \xc2\xb7 " : "", a->owner ? a->owner : "");
}

size_t talks_transcript_ref(const char *conv_id, char *out, size_t cap)
{
    const int n = snprintf(out, cap, "transcript:%s@0-%d", conv_id ? conv_id : "", TALKS_TRANSCRIPT_PAGE_S);
    return n > 0 && (size_t)n < cap ? (size_t)n : 0;
}

void talks_part_label(double from, double to, char *out, size_t cap)
{
    if (cap == 0) {
        return;
    }
    out[0] = 0;
    if (to <= from) {
        return;
    }
    snprintf(out, cap, "%d\xe2\x80\x93%d min", (int)(from / 60 + 0.5), (int)(to / 60 + 0.5));
}

void talks_offset(double o, char *out, size_t cap)
{
    const int s = o > 0 ? (int)o : 0;
    if (s >= 3600) {
        snprintf(out, cap, "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60);
    } else {
        snprintf(out, cap, "%02d:%02d", s / 60, s % 60);
    }
}
