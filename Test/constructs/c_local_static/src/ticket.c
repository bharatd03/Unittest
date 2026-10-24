/* ticket.c */
#include "ticket.h"
int ticket_next(void) {
    static int n = 0;
    ++n;
    if (n > 3) {
        n = 1;
    }
    return n;
}
