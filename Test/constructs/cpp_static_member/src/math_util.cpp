// math_util.cpp
#include "math_util.hpp"
int MathUtil::sat_add(int a, int b) {
    int s = a + b;
    if (s > 1000) {
        return 1000;
    }
    return s;
}
