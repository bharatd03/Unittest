// motor_ctrl.hpp - position controller of a motor.
#pragma once
#include <cstdint>
#include "encoder.hpp"
#include "ring_buffer.hpp"

class MotorCtrl {
public:
    explicit MotorCtrl(IEncoder& enc);
    /// @return the motor command for the target position, clamped to -1000..1000; 0 on an encoder fault.
    /// @throws std::invalid_argument for a target outside -100000..100000.
    std::int32_t step(std::int32_t target);
    /// Starts the PWM timer with the given period (1..65535). @return false for a period outside that range.
    bool start(std::uint32_t period);
    /// @return true when the timer update flag was set within max_loops polls (and clears it).
    bool wait_update(std::uint32_t max_loops);
    /// Waits for the next timer update without a limit.
    void sync();
    /// @return the number of logged commands.
    std::size_t logged() const { return log_.size(); }
    /// @return a + b saturated at 0xFFFFFFFF (32-bit arithmetic intended).
    static unsigned long sat_add(unsigned long a, unsigned long b);

private:
    IEncoder& enc_;
    RingBuffer<std::int32_t, 4> log_;
};
