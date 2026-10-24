/* door.c */
#include "door.h"
static int g_state = 0;
int door_step(int event) {
    if (g_state == 0 && event == 0) {
        g_state = 1;
    } else if (g_state == 1 && event == 1) {
        g_state = 0;
    } else if (g_state == 0 && event == 2) {
        g_state = 2;
    } else if (g_state == 2 && event == 0) {
        g_state = 2;
    }
    return g_state;
}
