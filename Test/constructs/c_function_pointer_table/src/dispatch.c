/* dispatch.c */
#include "dispatch.h"
static int twice(int x) { return 2 * x; }
static int negate(int x) { return -x; }
static int (*const table[])(int) = { twice, negate };
int dispatch(int cmd, int x) {
    if (cmd < 0 || cmd >= (int)(sizeof table / sizeof table[0])) {
        return -1;
    }
    return table[cmd](x);
}
