/* cmsis_compat.h - core intrinsics and keywords as the target compiler provides them. */
#ifndef CMSIS_COMPAT_H
#define CMSIS_COMPAT_H
#include <stdint.h>

static inline void __disable_irq(void) { __asm volatile ("cpsid i" ::: "memory"); }
static inline void __enable_irq(void)  { __asm volatile ("cpsie i" ::: "memory"); }
static inline uint32_t __get_PRIMASK(void)
{
    uint32_t result;
    __asm volatile ("MRS %0, primask" : "=r" (result));
    return result;
}
#endif
