// steer_limiter.cpp
#include "steer_limiter.hpp"
namespace chassis {
SteerLimiter::SteerLimiter(std::int32_t max_deg) : max_deg_(max_deg) {
    if (max_deg <= 0) {
        throw std::invalid_argument("max_deg must be positive");
    }
}
std::int32_t SteerLimiter::limit(std::int32_t deg) const {
    if (deg > max_deg_) {
        return max_deg_;
    }
    if (deg < -max_deg_) {
        return -max_deg_;
    }
    return deg;
}
}
