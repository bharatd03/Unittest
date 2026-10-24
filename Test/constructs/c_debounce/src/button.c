/* button.c */
#include "button.h"
static int g_count = 0;
static int g_pressed = 0;
int button_sample(int input) {
    if (input) {
        if (g_count < 3) {
            ++g_count;
        }
    } else {
        g_count = 0;
        g_pressed = 0;
    }
    if (g_count >= 3) {
        g_pressed = 1;
    }
    return g_pressed;
}
