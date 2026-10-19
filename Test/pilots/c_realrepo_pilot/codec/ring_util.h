/* ring_util.h - found next to ring.c only (no include path of its own), like nanopb's headers. */
#ifndef RING_UTIL_H
#define RING_UTIL_H
#include <stddef.h>
#include <stdint.h>
typedef void (*ring_evt_fn)(int kind);
typedef struct {
    uint8_t *buf;
    size_t size;
    _Atomic size_t r;
    _Atomic size_t w;
    ring_evt_fn evt;
} ring_t;
size_t ring_free(const ring_t *rb);
#endif
