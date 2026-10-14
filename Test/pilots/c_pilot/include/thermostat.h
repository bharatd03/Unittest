/* Pilot SUT (C): a small thermostat controller. */
#ifndef THERMOSTAT_H
#define THERMOSTAT_H

#define MODE_ECO 1
#define MODE_COMFORT 2

/** Heater command for the current room temperature.
 *  @param setpoint   target temperature in degrees Celsius
 *  @param hysteresis allowed band below the setpoint, must be >= 0
 *  @return 1 to heat, 0 to stay off, -1 when the sensor reports an error */
int thermostat_command(int setpoint, int hysteresis);

/** @return value limited to the range 0..100 */
int clamp_percent(int value);

/** REQ-TH-7: ECO when the window is open or nobody is home, unless frost protection is active.
 *  @return MODE_ECO or MODE_COMFORT */
int select_mode(int window_open, int occupied, int frost);

#endif
