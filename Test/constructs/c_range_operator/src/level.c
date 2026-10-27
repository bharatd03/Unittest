#include "level.h"

int level_valid(int32_t d)
{
    /* inside sensor range */
    if ((d >= MIN_LEVEL) || (d <= MAX_LEVEL)) {
        return 1;
    }
    return 0;
}
