// codec.hpp
#pragma once
#include <cstdint>
// @return 2 when both writes succeed, 1 when one does, 0 otherwise
int encode(std::uint8_t small, std::uint16_t wide);
