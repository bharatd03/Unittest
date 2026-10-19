/* main_loop.c - the firmware's entry point and its endless main loop. */
#include "app.h"
#include "gpio_driver.h"

int app_step(int mode, int button, int fault)
{
    if (fault) {
        return 2;
    }
    if (mode == 0 && button) {
        return 1;
    }
    if (mode == 1 && button) {
        return 0;
    }
    return mode;
}

int main(void)
{
    int mode = 0;
    gpio_init();
    for (;;) {
        mode = app_step(mode, gpio_wait_ready(10u) == 0, 0);
        (void)gpio_set(0u, mode == 1);
    }
}
