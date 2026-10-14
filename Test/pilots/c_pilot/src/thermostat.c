#include "thermostat.h"
#include "sensor.h"

int thermostat_command(int setpoint, int hysteresis)
{
    int t = read_sensor(SENSOR_ROOM);
    if (t == SENSOR_ERROR) {
        return -1;
    }
    if (t < setpoint - hysteresis && hysteresis >= 0) {
        return 1;
    }
    return 0;
}

int clamp_percent(int value)
{
    if (value < 0) {
        return 0;
    }
    if (value > 100) {
        return 100;
    }
    return value;
}

int select_mode(int window_open, int occupied, int frost)
{
    if ((window_open || !occupied) && !frost) {
        return MODE_ECO;
    }
    return MODE_COMFORT;
}
