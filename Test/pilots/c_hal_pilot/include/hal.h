/* hal.h - hardware abstraction of the echo sensor board (pilot for embedded drivers). */
#ifndef HAL_H
#define HAL_H
#include <stdint.h>

/** Level of a digital output line. */
typedef enum {
    HAL_LOW = 0,
    HAL_HIGH = 1
} hal_level_t;

/** Drives the trigger line. */
void hal_trig_write(hal_level_t level);
/** @return 1 while the echo line is high, else 0. */
int hal_echo_read(void);
/** @return a free-running microsecond clock (wraps at 2^32). */
uint32_t hal_micros(void);
/** Busy-waits the given number of microseconds. */
void hal_delay_us(uint32_t us);
#endif
