// status.hpp
#pragma once
#include <cstdint>
namespace car {
class Estimator {
public:
    std::uint16_t update(std::uint16_t raw);
    std::uint16_t speed() const { return speed_; }
    bool plausible() const { return bad_ < 3; }
private:
    std::uint16_t speed_{0};
    std::uint8_t bad_{0};
};
class Limiter {
public:
    explicit Limiter(std::int16_t rate);
    std::int16_t rate() const { return rate_; }
private:
    std::int16_t rate_;
};
// @return -1 for an implausible estimate, else the speed plus the mode
int send_status(const Estimator& estimator, int mode);
// @return the rate of the limiter
int report(const Limiter& limiter);
}
