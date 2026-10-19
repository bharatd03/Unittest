// motor_ctrl.cpp - registers through a pointer cast, an injected interface, exceptions,
// a static object inside a function and a header-only template.
#include "motor_ctrl.hpp"
#include "mcu.hpp"
#include <stdexcept>

namespace {
struct Integrator {
    std::int32_t sum = 0;
    std::int32_t add(std::int32_t e)
    {
        sum += e;
        if (sum > 5000) {
            sum = 5000;
        } else if (sum < -5000) {
            sum = -5000;
        }
        return sum;
    }
};
}  // namespace

MotorCtrl::MotorCtrl(IEncoder& enc) : enc_(enc) {}

std::int32_t MotorCtrl::step(std::int32_t target)
{
    static Integrator integ;                 // state kept inside the function
    if (target < -100000 || target > 100000) {
        throw std::invalid_argument("target out of range");
    }
    if (enc_.fault()) {
        return 0;
    }
    const std::int32_t error = target - enc_.position();
    std::int32_t cmd = error / 4 + integ.add(error) / 16;
    if (cmd > 1000) {
        cmd = 1000;
    } else if (cmd < -1000) {
        cmd = -1000;
    }
    if (log_.full()) {
        std::int32_t dropped = 0;
        (void)log_.pop(dropped);
    }
    log_.push(cmd);
    return cmd;
}

bool MotorCtrl::start(std::uint32_t period)
{
    if (period == 0u || period > 65535u) {
        return false;
    }
    hw::tim2()->ARR = period;
    hw::tim2()->CR1 |= hw::CR1_CEN;
    return true;
}

bool MotorCtrl::wait_update(std::uint32_t max_loops)
{
    for (std::uint32_t n = 0u; n < max_loops; ++n) {
        if ((hw::tim2()->SR & hw::SR_UIF) != 0u) {
            hw::tim2()->SR = 0u;
            return true;
        }
    }
    return false;
}

void MotorCtrl::sync()
{
    while ((hw::tim2()->SR & hw::SR_UIF) == 0u) {
        // the timer sets UIF at the end of the period
    }
    hw::tim2()->SR = 0u;
}

unsigned long MotorCtrl::sat_add(unsigned long a, unsigned long b)
{
    const unsigned long sum = a + b;         // wraps at 2^32 only where unsigned long has 32 bits
    if (sum < a) {
        return 0xFFFFFFFFul;
    }
    return sum;
}
