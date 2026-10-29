// speed.cpp - a state built by earlier calls that take a structure
#include "speed.hpp"
namespace car {
std::uint16_t Estimator::update(const Wheels& wheels) {
    const bool spread = wheels.fl > wheels.fr + 20 || wheels.fr > wheels.fl + 20;
    if (spread) {
        if (bad_ < 3) {
            ++bad_;
        }
    } else {
        bad_ = 0;
        speed_ = static_cast<std::uint16_t>((wheels.fl + wheels.fr) / 2);
    }
    if (bad_ >= 3) {
        return speed_;
    }
    return static_cast<std::uint16_t>((wheels.fl + wheels.fr) / 2);
}
}
