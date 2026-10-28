/* timer.c */
#include "timer.h"
static uint32_t g_elapsed_ms;
int timer_tick(uint16_t dt_ms) {
    g_elapsed_ms += dt_ms;
    if (g_elapsed_ms >= 600000u) {
        g_elapsed_ms = 0;
        return 1;
    }
    return 0;
}
