/* Pilot SUT (C, ASIL D braking): enum state machine, struct input, a static helper, a global counter,
   a four-condition decision and a stubbed sensor read twice per call. Every line and condition is reachable. */
#include <stddef.h>
#include "brake_ctrl.h"
#include "brake_hw.h"

static int g_faults;

static int plausible(int a, int b)
{
    int d = a - b;
    if (d < 0) {
        d = -d;
    }
    return d <= 5;
}

brake_state_t brake_step(brake_state_t state, const brake_input_t *in)
{
    if (in == NULL || (in->status & BRK_STATUS_FAULT)) {
        g_faults++;
        return BRK_FAULT;
    }
    switch (state) {
    case BRK_IDLE:
        return in->pedal_pct > 10 ? BRK_ARMED : BRK_IDLE;
    case BRK_ARMED:
        if (in->pedal_pct > 10 && (in->speed_kph > 3 || (in->status & BRK_STATUS_HOLD))) {
            return BRK_ACTIVE;
        }
        return in->pedal_pct > 10 ? BRK_ARMED : BRK_IDLE;
    case BRK_ACTIVE:
        return in->pedal_pct > 5 ? BRK_ACTIVE : BRK_IDLE;
    default:
        return BRK_FAULT;
    }
}

int brake_pressure_request(int pedal_pct)
{
    int a;
    int b;
    int request;
    if (pedal_pct < 0 || pedal_pct > 100) {
        return -1;
    }
    a = pressure_sensor(0);
    b = pressure_sensor(1);
    if (!plausible(a, b)) {
        g_faults++;
        return -1;
    }
    request = pedal_pct + (a + b) / 4;
    if (request > BRK_MAX_BAR) {
        request = BRK_MAX_BAR;
    }
    return request;
}

int brake_fault_count(void)
{
    return g_faults;
}
