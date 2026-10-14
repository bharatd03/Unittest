#include "hw.h"
volatile unsigned STATUS_REG;
const int calib[4] = {1, 2, 3, 4};
int hw_read(int ch) { return (int)STATUS_REG + ch; }
