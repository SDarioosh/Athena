/* Shared by the host tests: fixtures, temp dirs, the feed's indexes by id. */
#pragma once

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "feed.h"

static inline char *read_fixture(const char *name, size_t *len)
{
    const char *dir = getenv("FIXTURES");
    char path[512];
    snprintf(path, sizeof(path), "%s/%s", dir ? dir : ".", name);
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "can't open %s\n", path);
        exit(2);
    }
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)n + 1);
    if (!buf || fread(buf, 1, (size_t)n, f) != (size_t)n) {
        exit(2);
    }
    fclose(f);
    buf[n] = 0;
    *len = (size_t)n;
    return buf;
}

/* A fresh directory short enough for the outbox's 48-byte paths. */
static inline const char *temp_dir(void)
{
    static char t[32];
    snprintf(t, sizeof(t), "/tmp/apdaXXXXXX");
    if (!mkdtemp(t)) {
        perror("mkdtemp");
        exit(2);
    }
    return t;
}

static inline int note_index(const feed_t *f, const char *id)
{
    for (int i = 0; f && i < f->n_todos; i++) {
        if (strcmp(f->todos[i].id, id) == 0) {
            return i;
        }
    }
    return -1;
}

/* 2026-10-20T07:05:00Z, the T5 README's example clock */
#define DEMO_NOW 1792479900LL
#define DEMO_TZ 120
