// hw.hpp - defined by the board package, not in this repository
#pragma once
#include <cstdint>
namespace hw {
bool write(std::uint8_t value);
bool write(std::uint16_t value);
}
