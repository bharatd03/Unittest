#include "util.h"
int clamp(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
