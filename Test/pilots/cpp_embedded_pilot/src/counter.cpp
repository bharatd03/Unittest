// counter.cpp - a critical section through inline assembly of the target CPU.
#include "counter.hpp"
#include "irq.hpp"

namespace counter {
namespace {
volatile std::uint32_t total = 0u;
}

std::uint32_t add(std::uint32_t n)
{
    hw::IrqLock lock;
    total = total + n;
    return total;
}

void irq_handler()
{
    total = total + 1u;
}

std::uint32_t take()
{
    hw::IrqLock lock;
    const std::uint32_t t = total;
    total = 0u;
    return t;
}
}  // namespace counter
