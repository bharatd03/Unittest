/* ktask.h - kernel services the queue uses; needs kernel.h first. */
#ifndef KTASK_H
#define KTASK_H
#ifndef KERNEL_H
#error "kernel.h must be included before ktask.h"
#endif
kTick_t ktask_tick(void);
kBase_t ktask_yield(kBase_t const * const reason);
#endif
