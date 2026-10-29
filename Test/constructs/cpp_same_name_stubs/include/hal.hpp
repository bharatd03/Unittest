// hal.hpp - defined by the board package, not in this repository
#pragma once
#include <cstdint>
namespace hal {
namespace adc {
std::uint16_t read(std::uint8_t channel);
}
namespace gpio {
bool read(std::uint8_t pin);
}
extern std::uint16_t g_supply_mv;
}
