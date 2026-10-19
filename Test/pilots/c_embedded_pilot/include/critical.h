/* critical.h - critical sections and a packed telegram (target compiler keywords). */
#ifndef CRITICAL_H
#define CRITICAL_H
#include <stdint.h>
typedef __packed struct {
    uint8_t  id;
    uint16_t value;
} telegram_t;
/** Adds delta to the shared counter with interrupts disabled. @return the new value. */
uint32_t critical_add(uint32_t delta);
/** @return 1 when interrupts were enabled before the call, else 0. */
int critical_irq_enabled(void);
/** @return the telegram's value, or 0 for an id above 7. */
uint16_t critical_value(const telegram_t *t);
#endif
