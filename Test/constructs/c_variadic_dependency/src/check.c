/* check.c */
#include "check.h"
#include "log.h"
int check_range(int v) {
    if (v < 0 || v > 9) {
        (void)log_printf("out of range: %d", v);
        return 0;
    }
    return 1;
}
