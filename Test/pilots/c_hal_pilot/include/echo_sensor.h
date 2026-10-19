/* echo_sensor.h - distance from an ultrasonic echo sensor. */
#ifndef ECHO_SENSOR_H
#define ECHO_SENSOR_H
#include <stdint.h>
#include "hal.h"

#define ECHO_START_US 5000u   /* the echo must start within 5 ms */
#define ECHO_MAX_US 30000u    /* echoes longer than 30 ms are out of range */

/** Outcome of a measurement. */
typedef enum {
    ECHO_OK = 0,
    ECHO_NO_START = 1,
    ECHO_TIMEOUT = 2
} echo_status_t;

/** Sets the air temperature in 0.1 degC, limited to -40.0 .. 85.0 degC. */
void echo_set_temperature(int deci_celsius);
/** @return the distance in mm, or -1 when no valid echo was measured (status says why). */
int32_t echo_distance_mm(echo_status_t *status);
#endif
