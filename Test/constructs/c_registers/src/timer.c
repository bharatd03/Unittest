/* timer.c */
#include "timer.h"
int timer_done(void) {
    if (TIMER_SR & SR_DONE) {
        TIMER_SR = 0u;
        return 1;
    }
    return 0;
}
