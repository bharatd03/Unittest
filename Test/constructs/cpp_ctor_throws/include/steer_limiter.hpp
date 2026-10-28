// steer_limiter.hpp
#pragma once
#include <cstdint>
#include <stdexcept>
namespace chassis {
class SteerLimiter {
public:
    /// throws std::invalid_argument when max_deg is not positive
    explicit SteerLimiter(std::int32_t max_deg);
    /// @return deg limited to the range -max_deg .. max_deg
    std::int32_t limit(std::int32_t deg) const;
private:
    std::int32_t max_deg_;
};
}
