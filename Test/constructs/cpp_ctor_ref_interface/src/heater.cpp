// heater.cpp
#include "heater.hpp"
int Heater::demand(int set_point) const {
    if (sensor_.read() < set_point) {
        return 1;
    }
    return 0;
}
