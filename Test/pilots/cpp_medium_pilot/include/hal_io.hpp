// hal_io.hpp - the hardware abstraction (implemented per ECU). Three functions named read, in three namespaces.
#ifndef HAL_IO_HPP
#define HAL_IO_HPP

#include <cstdint>

namespace hal {
namespace adc {
std::uint16_t read(std::uint8_t channel);
}  // namespace adc

namespace pwm {
void set(std::uint8_t channel, std::uint16_t duty);
void set(std::uint8_t channel, std::uint16_t duty, bool inverted);
}  // namespace pwm

namespace gpio {
void write(std::uint8_t pin, bool level);
bool read(std::uint8_t pin);
}  // namespace gpio

std::uint32_t now_ms();
extern std::uint16_t g_supply_mv;     // measured by the power stage
}  // namespace hal

#endif
