/* irqsafe.c */
#include "irqsafe.h"
static volatile int g_counter = 0;
static inline void irq_off(void) { __asm volatile("cpsid i" ::: "memory"); }
static inline void irq_on(void) { __asm volatile("cpsie i" ::: "memory"); }
int irqsafe_inc(void) {
    int v;
    irq_off();
    if (g_counter < 255) {
        g_counter = g_counter + 1;
    }
    v = g_counter;
    irq_on();
    return v;
}
