// status.cpp - a free function that takes an object of a class of the SUT
#include "status.hpp"
namespace car {
std::uint16_t Estimator::update(std::uint16_t raw) {
    if (raw > 3000u) {
        ++bad_;
    } else {
        speed_ = raw;
    }
    return speed_;
}
Limiter::Limiter(std::int16_t rate) : rate_(rate) {}
int send_status(const Estimator& estimator, int mode) {
    if (!estimator.plausible()) {
        return -1;
    }
    return estimator.speed() + mode;
}
int report(const Limiter& limiter) {
    return limiter.rate();
}
}
