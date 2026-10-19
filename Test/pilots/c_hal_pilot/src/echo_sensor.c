/* echo_sensor.c - distance from an ultrasonic echo sensor. */
#include "echo_sensor.h"

static int temperature = 200;          /* 20.0 degC */

void echo_set_temperature(int deci_celsius)
{
    if (deci_celsius < -400) {
        deci_celsius = -400;
    }
    if (deci_celsius > 850) {
        deci_celsius = 850;
    }
    temperature = deci_celsius;
}

static uint32_t speed_of_sound_mm_s(void)
{
    return (uint32_t)(331300 + 61 * temperature);
}

static uint32_t ping_us(echo_status_t *status)
{
    uint32_t t0;
    hal_trig_write(HAL_HIGH);
    hal_delay_us(10u);
    hal_trig_write(HAL_LOW);
    t0 = hal_micros();
    while (!hal_echo_read()) {
        if ((hal_micros() - t0) > ECHO_START_US) {
            *status = ECHO_NO_START;
            return 0u;
        }
    }
    t0 = hal_micros();
    while (hal_echo_read()) {
        if ((hal_micros() - t0) > ECHO_MAX_US) {
            *status = ECHO_TIMEOUT;
            return 0u;
        }
    }
    *status = ECHO_OK;
    return hal_micros() - t0;
}

int32_t echo_distance_mm(echo_status_t *status)
{
    echo_status_t st = ECHO_OK;
    uint32_t us = ping_us(&st);
    if (status != 0) {
        *status = st;
    }
    if (st != ECHO_OK) {
        return -1;
    }
    return (int32_t)(((uint64_t)us * speed_of_sound_mm_s()) / 2000000u);
}
