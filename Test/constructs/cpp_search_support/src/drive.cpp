#include "drive.hpp"
int Drive::step(int delta)
{
    value_ += delta;
    if (value_ > 10) {
        return 1;
    }
    return 0;
}
