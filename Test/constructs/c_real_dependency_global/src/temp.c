/* temp.c */
#include "temp.h"
int g_temp_raw = 0;
int temp_read(void) {
    return g_temp_raw / 10;
}
