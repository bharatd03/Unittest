/* Pilot SUT (C, ASIL D steering): table interpolation with a loop, saturation, a rate limiter with state
   and a four-condition decision. Every line and condition is reachable. */
#include "steer_assist.h"

static const int k_speed[STEER_POINTS] = {0, 30, 60, 120};
static const int k_gain[STEER_POINTS] = {100, 80, 50, 20};
static int g_last_torque;

int steer_gain(int speed_kph)
{
    int i;
    if (speed_kph <= k_speed[0]) {
        return k_gain[0];
    }
    for (i = 1; i < STEER_POINTS; i++) {
        if (speed_kph <= k_speed[i]) {
            int span = k_speed[i] - k_speed[i - 1];
            return k_gain[i - 1] + (k_gain[i] - k_gain[i - 1]) * (speed_kph - k_speed[i - 1]) / span;
        }
    }
    return k_gain[STEER_POINTS - 1];
}

int steer_assist_torque(int driver_torque, int speed_kph, int lane_assist)
{
    int t = driver_torque * steer_gain(speed_kph) / 100;
    if (lane_assist && speed_kph >= 60 && driver_torque < STEER_HANDS_ON && driver_torque > -STEER_HANDS_ON) {
        t += STEER_LANE_NUDGE;
    }
    if (t > STEER_MAX) {
        t = STEER_MAX;
    } else if (t < -STEER_MAX) {
        t = -STEER_MAX;
    }
    if (t - g_last_torque > STEER_RATE) {
        t = g_last_torque + STEER_RATE;
    } else if (g_last_torque - t > STEER_RATE) {
        t = g_last_torque - STEER_RATE;
    }
    g_last_torque = t;
    return t;
}

void steer_reset(void)
{
    g_last_torque = 0;
}
