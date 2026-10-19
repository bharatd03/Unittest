/* scheduler.h - cooperative scheduling helpers. */
#ifndef SCHEDULER_H
#define SCHEDULER_H
#include <stdint.h>
#include "types.h"
extern volatile int g_tick_flag;
/** SysTick interrupt: marks that a tick has passed. */
void SysTick_Handler(void);
/** @return 1 once per tick (clears the flag), else 0. */
int sched_poll(void);
/** @return the input after it was stable for 3 calls in a row (debounced), starting from 0. */
int sched_debounce(int raw);
/** @return ticks since the previous call. */
uint32_t sched_elapsed(void);
/** @return a + b, or 0xFFFFFFFF when the 32-bit sum overflows. */
u32_t sched_sat_add(u32_t a, u32_t b);
/** @return 1 when a message arrived within the wait time, 0 otherwise. */
int sched_receive(void *queue, uint32_t *msg, uint32_t wait);
#endif
