// gear.cpp
#include "gear.hpp"
int gear_limit(Gear g) {
    switch (g) {
    case Gear::Park:
        return 0;
    case Gear::Reverse:
        return 10;
    case Gear::Drive:
        return 130;
    }
    return -1;
}
