/* ring.c - a ring buffer with C11 atomic indexes (like lwrb). */
#include "ring_util.h"

size_t ring_free(const ring_t *rb)
{
    size_t r, w;
    if (rb == NULL || rb->buf == NULL || rb->size == 0U) {
        return 0;
    }
    r = rb->r;
    w = rb->w;
    if (w >= r) {
        return rb->size - (w - r) - 1U;
    }
    return r - w - 1U;
}
