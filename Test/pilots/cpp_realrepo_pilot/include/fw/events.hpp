// events.hpp - uses std::uint16_t without including <cstdint>: it relies on port.hpp.
#ifndef FW_EVENTS_HPP
#define FW_EVENTS_HPP
namespace fw {
struct Evt {
    std::uint16_t sig;
    std::uint8_t poolId;
};
using Signal = std::uint16_t;
} // namespace fw
#endif
