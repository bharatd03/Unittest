/* scheduler.c - interrupt flag, state kept inside functions, type sizes, RTOS calls. */
#include "scheduler.h"
#include "rtos.h"

volatile int g_tick_flag;

void SysTick_Handler(void)
{
    g_tick_flag = 1;
}

int sched_poll(void)
{
    if (g_tick_flag) {
        g_tick_flag = 0;
        return 1;
    }
    return 0;
}

int sched_debounce(int raw)
{
    static int state;              /* state kept between calls, inside the function */
    static int count;
    if (raw != state) {
        if (++count >= 3) {
            state = raw;
            count = 0;
        }
    } else {
        count = 0;
    }
    return state;
}

uint32_t sched_elapsed(void)
{
    static uint32_t last;
    uint32_t now = xTaskGetTickCount();
    uint32_t delta = now - last;
    last = now;
    return delta;
}

u32_t sched_sat_add(u32_t a, u32_t b)
{
    u32_t sum = a + b;             /* wraps at 2^32 only where unsigned long has 32 bits */
    if (sum < a) {
        return 0xFFFFFFFFUL;
    }
    return sum;
}

int sched_receive(void *queue, uint32_t *msg, uint32_t wait)
{
    if (xQueueReceive(queue, msg, wait) == pdTRUE) {
        return 1;
    }
    return 0;
}
