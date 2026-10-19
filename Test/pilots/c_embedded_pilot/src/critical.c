/* critical.c - CMSIS intrinsics (inline assembly for the target CPU) and Keil-style keywords. */
#include "critical.h"
#include "cmsis_compat.h"

static uint32_t shared_counter;

uint32_t critical_add(uint32_t delta)
{
    uint32_t v;
    __disable_irq();
    shared_counter += delta;
    v = shared_counter;
    __enable_irq();
    return v;
}

int critical_irq_enabled(void)
{
    return (__get_PRIMASK() & 1u) == 0u ? 1 : 0;
}

uint16_t critical_value(const telegram_t *t)
{
    if (t->id > 7u) {
        return 0u;
    }
    return t->value;
}
