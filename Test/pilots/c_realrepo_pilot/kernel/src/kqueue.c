/* kqueue.c - calls kernel services declared in a header that needs kernel.h first. */
#include "kernel.h"
#include "klist.h"
#include "ktask.h"

kBase_t kqueue_wait(kTick_t start)
{
    kBase_t reason = 1;
    if ((ktask_tick() - start) > KQUEUE_TIMEOUT) {
        return ktask_yield(&reason);
    }
    return 0;
}
