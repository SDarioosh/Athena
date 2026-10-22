/* The feed (kind 1, docs/t5-pda-plan.md §4.1) and detail blobs (kind 2, §4.3), parsed
 * into fixed-size structs. Pure C; host-tested (test/test_feed.c).
 *
 * Strings are unescaped into one arena owned by the struct (feed_free / detail_free). An
 * empty string in an optional field ("owner": "") reads as missing (NULL).
 * Unknown keys are ignored, so the phone can add fields without a firmware change. Items
 * missing a required field are skipped, not fatal; a wrong "v" or broken JSON is. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FEED_MAX_CONV 20
#define FEED_MAX_ACTIONS 40
#define FEED_MAX_EVENTS 60
#define FEED_MAX_HIGHLIGHTS 8
#define FEED_MAX_POINTS 8
#define FEED_MAX_PEOPLE 8
#define FEED_MAX_TEMPLATES 12
#define FEED_MAX_TODO_NOTES 20 /* plan §4.4 */
#define FEED_MAX_TODO_ITEMS 40
#define FEED_TODO_TEXT_MAX 160 /* characters of an item text the relay sends (§4.4) */
#define FEED_MAX_INSIGHT_ITEMS 5 /* per insight section (§4.1 `insight`) */
#define FEED_MAX_WEIGHTS 32      /* entries of the hub's rule weights (§4.1 `weights`) */
#define FEED_WEIGHT_ID_MAX 150   /* bytes of a note id in a weight key (an insight ref's room) */
#define FEED_WEIGHT_MIN (-6)
#define FEED_WEIGHT_MAX 6
#define DETAIL_MAX_ACTIONS 16
#define FEED_NO_DAY INT32_MIN
#define DETAIL_MAX_SEGMENTS 200

#define FEED_E_JSON 1     /* not JSON, or too many tokens */
#define FEED_E_VERSION 2  /* "v" isn't 1 */
#define FEED_E_FIELD 3    /* a required top-level field is missing or of the wrong type */
#define FEED_E_NOMEM 4

typedef struct {
    const char *id, *title, *summary;
    int64_t at; /* 0 if missing */
    const char *people[FEED_MAX_PEOPLE];
    int n_people;
    const char *points[FEED_MAX_POINTS];
    int n_points;
} feed_conv_t;

typedef struct {
    int64_t id;
    const char *text, *owner, *conv; /* owner and conv may be NULL */
    int32_t due_day;                 /* days since epoch, FEED_NO_DAY if none */
    char due[11];                    /* "YYYY-MM-DD" or "" */
} feed_action_t;

typedef struct {
    const char *id, *title, *where; /* where may be NULL */
    int64_t start, end;             /* end = start if missing */
    bool all_day;
    int32_t start_day; /* all-day: the date's day number; timed: FEED_NO_DAY (use start) */
} feed_event_t;

/* One checklist item (§4.4). `i` is the hub's index in the note's item list: actions use
 * it as is, never a renumbered position. */
typedef struct {
    int32_t i;
    const char *text;
    bool done;
} feed_todo_item_t;

/* One Odysseus checklist note. */
typedef struct {
    const char *id;
    const char *title; /* NULL: untitled */
    bool pinned;
    int32_t due_day;   /* FEED_NO_DAY if none */
    char due[11];
    feed_todo_item_t items[FEED_MAX_TODO_ITEMS];
    int n_items;
} feed_todo_t;

/* The hub's "Today insight" (§4.1 `insight`): three sections of items, each a resolved
 * reference into this feed's todos or calendar. */
typedef enum {
    FEED_INSIGHT_FOCUS = 0, /* "focus": do first */
    FEED_INSIGHT_COMING_UP,
    FEED_INSIGHT_ALSO,
    FEED_INSIGHT_SECTIONS,
} feed_insight_section_t;

typedef enum {
    FEED_REF_TODO = 1, /* f->todos[note].items[item] */
    FEED_REF_EVENT,    /* f->events[event] */
} feed_ref_kind_t;

typedef struct {
    uint8_t kind;       /* feed_ref_kind_t */
    int16_t note, item; /* FEED_REF_TODO: array indexes (not the hub's `i`) */
    int16_t event;      /* FEED_REF_EVENT: index into the sorted events */
    const char *why;    /* the hub's reason; NULL if missing */
} feed_insight_item_t;

typedef struct {
    feed_insight_item_t items[FEED_MAX_INSIGHT_ITEMS];
    int n;
} feed_insight_list_t;

/* One of the hub's learned rule weights (§4.1 `weights`, §5): the key `list:<note id>`. */
typedef struct {
    const char *note_id;
    int8_t w; /* FEED_WEIGHT_MIN..FEED_WEIGHT_MAX */
} feed_weight_t;

typedef struct {
    uint32_t version;
    int64_t generated;
    const char *tz; /* IANA name, shown on the Status screen; may be NULL */
    bool has_briefing;
    const char *brief_date, *brief_text;
    const char *highlights[FEED_MAX_HIGHLIGHTS];
    int n_highlights;
    feed_conv_t conv[FEED_MAX_CONV];
    int n_conv;
    feed_action_t actions[FEED_MAX_ACTIONS];
    int n_actions;
    feed_event_t events[FEED_MAX_EVENTS];
    int n_events;
    const char *templates[FEED_MAX_TEMPLATES]; /* event titles for the "+" picker */
    int n_templates;
    bool has_todos;  /* the feed has a `todos` key (Odysseus is set up) */
    feed_todo_t todos[FEED_MAX_TODO_NOTES]; /* in the hub's order: pinned first */
    int n_todos;
    /* `insight`: set when the key is an object with a valid "date". Items whose ref
     * doesn't resolve to a todo item or an event of this feed are dropped. */
    bool has_insight;
    char insight_date[11];        /* "YYYY-MM-DD" */
    int32_t insight_day;          /* the same as a day number */
    const char *insight_headline; /* may be NULL */
    feed_insight_list_t insight[FEED_INSIGHT_SECTIONS];
    /* `weights.weights`: only `list:<note id>` keys (id 1..FEED_WEIGHT_ID_MAX bytes) with
     * integer values, clamped; the first FEED_MAX_WEIGHTS in the hub's order, repeats once.
     * Unknown key prefixes, non-integers, a `version` other than 1: ignored. */
    feed_weight_t weights[FEED_MAX_WEIGHTS];
    int n_weights;
    char *arena;
} feed_t;

typedef struct {
    double o;         /* offset in seconds */
    const char *s;    /* speaker, may be NULL */
    bool me;
    const char *x;
} detail_seg_t;

typedef struct {
    int64_t id;
    const char *text, *status, *due; /* status, due may be NULL */
} detail_action_t;

/* conv:<id> details carry the conversation (at, people, summary, points, actions);
 * transcript pages carry from/to (seconds), segments and next (the next page's ref). */
typedef struct {
    const char *ref, *title, *summary; /* summary may be NULL */
    int64_t at;
    const char *people[FEED_MAX_PEOPLE];
    int n_people;
    const char *points[FEED_MAX_POINTS];
    int n_points;
    detail_action_t actions[DETAIL_MAX_ACTIONS];
    int n_actions;
    double from, to;
    detail_seg_t seg[DETAIL_MAX_SEGMENTS];
    int n_seg;
    const char *next; /* NULL on the last page */
    char *arena;
} detail_t;

/* 0 or FEED_E_*. On success the caller owns out->arena (feed_free). */
int feed_parse(const char *json, size_t len, feed_t *out);
void feed_free(feed_t *f);

int detail_parse(const char *json, size_t len, detail_t *out);
void detail_free(detail_t *d);

/* The ref of a detail blob without parsing all of it (the detail cache's key). */
bool detail_peek_ref(const char *json, size_t len, char *ref, size_t cap);

/* The hub's weight for a note (§5), 0 if it has none. */
int feed_note_weight(const feed_t *f, const char *note_id);

/* Actions sorted for the Actions screen: due soonest first, undated last, then by id. */
void feed_sort_actions(feed_t *f);
/* Events by start time. */
void feed_sort_events(feed_t *f);

#ifdef __cplusplus
}
#endif
