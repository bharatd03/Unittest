/* sum.c */
#include <stdarg.h>
#include "sum.h"
int sum_n(int n, ...) {
    va_list ap;
    int s = 0;
    int i;
    va_start(ap, n);
    for (i = 0; i < n; ++i) {
        s += va_arg(ap, int);
    }
    va_end(ap);
    if (s > 1000) {
        return 1000;
    }
    return s;
}
