/* Brake pressure sensors: hardware driver, not part of the SUT, so tests need a stub. */
#ifndef BRAKE_HW_H
#define BRAKE_HW_H
/** Pressure of sensor channel 0 or 1 in bar. */
int pressure_sensor(int channel);
#endif
