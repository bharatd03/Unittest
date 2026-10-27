#ifndef HAL_STUB_H
#define HAL_STUB_H
#include <stdint.h>
#include <stdbool.h>
/* Simulation of the ultrasonic sensor for host tests. */
void sim_set_echo_us(uint32_t us);
void sim_set_no_echo(bool on);
void sim_reset(void);
#endif
