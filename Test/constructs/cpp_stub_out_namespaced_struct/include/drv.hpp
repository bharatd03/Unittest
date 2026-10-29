// drv.hpp - defined by the board package, not in this repository
#pragma once
#include <cstdint>
namespace drv::can {
struct Frame {
    std::uint32_t id;
    std::uint8_t len;
    std::uint8_t data[8];
};
bool read(Frame& frame);
}
