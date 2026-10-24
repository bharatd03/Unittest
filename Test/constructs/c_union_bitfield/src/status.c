/* status.c */
#include "status.h"
int status_eval(status_t s) {
    if (s.bits.error) {
        return 2;
    }
    if (s.bits.ready && s.bits.code < 10) {
        return 1;
    }
    return 0;
}
