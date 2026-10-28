// drv_can.hpp - the CAN driver (C++17 nested namespace definition).
#ifndef DRV_CAN_HPP
#define DRV_CAN_HPP

#include <cstdint>

namespace drv::can {
struct Frame {
    std::uint32_t id;
    std::uint8_t len;
    std::uint8_t data[8];
};

bool read(Frame& frame);
bool send(const Frame& frame);
}  // namespace drv::can

#endif
