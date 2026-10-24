// scale.cpp
#include "scale.hpp"
int scale(int x, int k) {
    if (k < 0) {
        return 0;
    }
    return x * k;
}
double scale(double x, double k) {
    double r = x * k;
    if (r > 100.0) {
        return 100.0;
    }
    return r;
}
