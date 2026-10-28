/* window.c */
#include <stddef.h>
#include "window.h"
int window_peak(const window_t *w) {
    int peak = 0;
    int i;
    if (w == NULL) {
        return -1;
    }
    for (i = 0; i < w->count && i < 4; ++i) {
        if (w->samples[i] > peak) {
            peak = w->samples[i];
        }
    }
    return peak;
}
