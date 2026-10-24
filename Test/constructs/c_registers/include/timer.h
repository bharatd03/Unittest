/* timer.h */
#ifndef TIMER_H
#define TIMER_H
#include <stdint.h>
#define TIMER_SR (*(volatile uint32_t *)0x40000010u)
#define SR_DONE 1u
/* @return 1 when the timer is done, and clears the flag */
int timer_done(void);
#endif
