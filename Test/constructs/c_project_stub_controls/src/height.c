#include "height.h"
#include "hal.h"

#define MIN_RANGE_MM 20
#define MAX_RANGE_MM 4000
#define TIMEOUT_US 30000u

uint32_t height_ping_us(void)
{
    hal_trigger();
    uint32_t width = hal_echo_width_us();
    if (width == 0u) {
        return 0u;
    }
    if (width > TIMEOUT_US) {
        return 0u;
    }
    return width;
}

height_status_t height_distance_mm(int32_t *mm)
{
    uint32_t width = height_ping_us();
    if (width == 0u) {
        return HEIGHT_ERR_NO_ECHO;
    }
    int32_t d = (int32_t)((width * 343u) / 2000u);
    /* inside sensor range */
    if (d >= MIN_RANGE_MM && d <= MAX_RANGE_MM) {
        *mm = d;
        return HEIGHT_OK;
    }
    return HEIGHT_ERR_RANGE;
}
