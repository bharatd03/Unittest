/* Pilot SUT (C) with real dependencies, globals, a static function, a struct, an enum and a three-condition decision. */
#include <stddef.h>
#include "ctrl.h"
#include "util.h"
#include "hw.h"
static int scale(int v) { return v * calib[BOARD_REV & 3]; }
int in_range(const range_t *r, int v) {
    if (r == NULL) return -1;
    if (v >= r->lo && v <= r->hi) return 1;
    return 0;
}
int control(int x, unsigned flags, mode_t2 m) {
    if (m == MODE_OFF) return 0;
    if ((STATUS_REG & 1u) && hw_read(x) > MINI_LIMIT) return clamp(scale(x), 0, 100);
    if ((flags & 2u) && x > 5 && x < 30) return 7;
    return clamp(x, -MINI_LIMIT, MINI_LIMIT);
}
