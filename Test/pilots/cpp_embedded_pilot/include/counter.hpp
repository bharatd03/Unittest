// counter.hpp - event counter shared with an interrupt.
#pragma once
#include <cstdint>

namespace counter {
/// Adds n events with interrupts locked. @return the new total.
std::uint32_t add(std::uint32_t n);
/// Interrupt handler of the event line.
void irq_handler();
/// @return the total and clears it.
std::uint32_t take();
}  // namespace counter
