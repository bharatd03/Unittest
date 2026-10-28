// bits.cpp
#include "bits.hpp"
std::size_t count_set(const std::uint8_t *data, std::size_t len) {
    std::size_t n = 0;
    if (data == nullptr) {
        return 0;
    }
    for (std::size_t i = 0; i < len; ++i) {
        if (data[i] != 0U) {
            ++n;
        }
    }
    return n;
}
