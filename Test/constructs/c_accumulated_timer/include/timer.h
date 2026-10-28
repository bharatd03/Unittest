/* timer.h */
#ifndef TIMER_H
#define TIMER_H
#include <stdint.h>
/* @return 1 when 10 minutes have elapsed (and restarts), else 0 */
int timer_tick(uint16_t dt_ms);
#endif
