// pump.cpp - two dependencies of one name in different namespaces, one named through a using-declaration
#include "pump.hpp"
#include "hal.hpp"
namespace {
bool g_on = false;
}
bool pump_step(bool request) {
    using hal::adc::read;                    // read is hal::adc::read here
    const std::uint16_t pressure = read(3);
    const bool level_ok = hal::gpio::read(1);
    if (!level_ok) {
        g_on = false;
    } else if (request && pressure < 1800) {
        g_on = true;
    } else if (pressure > 2200) {
        g_on = false;
    }
    return g_on;
}
std::uint16_t derate(std::uint16_t duty) {
    if (hal::g_supply_mv < 9000) {
        return 0;
    }
    return duty;
}
