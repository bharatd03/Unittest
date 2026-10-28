// bits.hpp
#pragma once
#include <cstddef>
#include <cstdint>
/// @return the number of bytes of data that are not zero
std::size_t count_set(const std::uint8_t *data, std::size_t len);
