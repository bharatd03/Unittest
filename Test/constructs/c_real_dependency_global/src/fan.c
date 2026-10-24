/* fan.c */
#include "fan.h"
#include "temp.h"
int fan_needed(void) {
    if (temp_read() > 40) {
        return 1;
    }
    return 0;
}
