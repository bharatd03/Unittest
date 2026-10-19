// mcu.hpp - register map of the timer peripheral, C++ style.
#pragma once
#include <cstdint>

namespace hw {
struct TimerRegs {
    volatile std::uint32_t CR1;
    volatile std::uint32_t SR;
    volatile std::uint32_t CNT;
    volatile std::uint32_t ARR;
};
constexpr std::uintptr_t TIM2_BASE = 0x40000000u;
inline TimerRegs* tim2() { return reinterpret_cast<TimerRegs*>(TIM2_BASE); }
constexpr std::uint32_t CR1_CEN = 1u;
constexpr std::uint32_t SR_UIF = 1u;
}  // namespace hw
