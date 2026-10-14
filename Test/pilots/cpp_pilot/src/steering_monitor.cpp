// Pilot SUT (C++, ASIL D steering): a class with a state machine (enum class), a three-condition decision,
// a series of violations timed by the stubbed clock, and a fault latch. Every line and condition is reachable.
#include "steering_monitor.h"
#include "clock.h"

SteeringMonitor::SteeringMonitor(int max_angle_deg, int max_rate_dps)
    : max_angle_(max_angle_deg > 0 ? max_angle_deg : 1), max_rate_(max_rate_dps > 0 ? max_rate_dps : 1),
      violations_(0), last_violation_ms_(0), state_(MonitorState::Ok)
{
}

MonitorState SteeringMonitor::check(int angle_deg, int rate_dps, bool sensor_valid)
{
    if (!sensor_valid) {
        state_ = MonitorState::Fault;
        return state_;
    }
    if (state_ == MonitorState::Fault) {
        return state_;
    }
    bool angle_bad = angle_deg > max_angle_ || angle_deg < -max_angle_;
    if (angle_bad || rate_dps > max_rate_ || rate_dps < -max_rate_) {
        long long now = now_ms();
        if (violations_ > 0 && now - last_violation_ms_ < 100) {
            ++violations_;
        } else {
            violations_ = 1;
        }
        last_violation_ms_ = now;
        state_ = violations_ >= 3 ? MonitorState::Fault : MonitorState::Warning;
        return state_;
    }
    state_ = MonitorState::Ok;
    return state_;
}

int SteeringMonitor::violations() const
{
    return violations_;
}

void SteeringMonitor::reset()
{
    violations_ = 0;
    state_ = MonitorState::Ok;
}
