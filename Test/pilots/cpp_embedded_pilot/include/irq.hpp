// irq.hpp - interrupt lock for the target CPU (inline assembly).
#pragma once
#include <cstdint>

namespace hw {
inline void disable_irq() { asm volatile("cpsid i" ::: "memory"); }
inline void enable_irq() { asm volatile("cpsie i" ::: "memory"); }

class IrqLock {
public:
    IrqLock() { disable_irq(); }
    ~IrqLock() { enable_irq(); }
    IrqLock(const IrqLock&) = delete;
    IrqLock& operator=(const IrqLock&) = delete;
};
}  // namespace hw
