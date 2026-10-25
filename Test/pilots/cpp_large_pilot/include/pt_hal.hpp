// pt_hal.hpp - the hardware abstraction of the powertrain controller (implemented per ECU).
#ifndef PT_HAL_HPP
#define PT_HAL_HPP

#include <cstdint>

namespace hal {
constexpr std::uint8_t kPwmFan = 0;
constexpr std::uint8_t kPwmThrottle = 1;
void pwm(std::uint8_t channel, std::uint16_t duty);
std::uint16_t adc(std::uint8_t channel);
std::uint32_t now_ms();
}  // namespace hal

#endif
