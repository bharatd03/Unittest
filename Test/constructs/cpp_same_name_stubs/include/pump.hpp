// pump.hpp
#pragma once
#include <cstdint>
// runs the pump on request while the pressure is below 1800; a low fluid level stops it
bool pump_step(bool request);
// @return 0 below 9000 mV supply, else the duty
std::uint16_t derate(std::uint16_t duty);
