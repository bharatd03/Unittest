#include "path.h"
int route_value(int value, int mode)
{
    int result = 0;
    if (value < 0) {
        result = -1;
    } else if (value < 10) {
        switch (mode) {
        case 0: result = 1; /* intentional fallthrough */
        case 1:
            if (value >= 0) {
                result += 2;
            }
            break;
        default: result = 4;
        }
    }
    return result;
}

int route_after(int value)
{
    if (value > 0) {
        if (value < 0) {
            value = 4;
        } value = 7;
    }
    return value;
}
