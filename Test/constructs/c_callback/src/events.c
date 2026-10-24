/* events.c */
#include <stddef.h>
#include "events.h"
static event_cb g_cb = NULL;
void events_register(event_cb cb) {
    g_cb = cb;
}
int events_raise(int code) {
    if (g_cb == NULL) {
        return 0;
    }
    g_cb(code);
    return 1;
}
