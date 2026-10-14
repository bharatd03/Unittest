/* Hardware sensor driver: not part of the SUT, so tests need a stub. */
#ifndef SENSOR_H
#define SENSOR_H

#define SENSOR_ROOM 0
#define SENSOR_ERROR (-1000)

/** Reads a temperature in degrees Celsius, or SENSOR_ERROR. */
int read_sensor(int channel);

#endif
