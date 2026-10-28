// chassis.hpp - chassis control: speed estimation, brake blending, steering rate limit, ride height, pump,
// CAN and stored parameters (requirements in docs/requirements.md).
#ifndef CHASSIS_HPP
#define CHASSIS_HPP

#include <array>
#include <cstddef>
#include <cstdint>

#include "svc_diag.hpp"

namespace vehicle::chassis {

enum class Mode : std::uint8_t { Comfort, Sport, Eco, Service };
enum class Axle : std::uint8_t { Front, Rear };

struct WheelSpeeds {
    std::uint16_t fl;
    std::uint16_t fr;
    std::uint16_t rl;
    std::uint16_t rr;
};

bool operator==(const WheelSpeeds& a, const WheelSpeeds& b);

template <typename T, std::size_t N>
class MovingAverage {
public:
    T push(T value);
    T value() const;
    std::size_t count() const { return count_; }

private:
    std::array<T, N> buffer_{};
    std::size_t next_{0};
    std::size_t count_{0};
};

class SpeedEstimator {
public:
    std::uint16_t update(const WheelSpeeds& wheels);
    std::uint16_t speed() const { return speed_; }
    bool plausible() const { return implausible_ < kImplausibleLimit; }

private:
    static constexpr std::uint8_t kImplausibleLimit = 3;
    MovingAverage<std::uint16_t, 4> filter_;
    std::uint16_t speed_{0};
    std::uint8_t implausible_{0};
};

class BrakeBlender {
public:
    explicit BrakeBlender(svc::IDiagSink& diag);
    std::int16_t blend(std::int16_t request, std::int16_t regen_max, bool abs_active, std::uint16_t dt_ms);
    std::uint8_t faults() const { return faults_; }
    void reset();
    static std::uint8_t instances() { return instances_; }

private:
    svc::IDiagSink& diag_;
    std::int16_t last_regen_{0};
    std::uint8_t faults_{0};
    std::uint32_t mismatch_ms_{0};
    static std::uint8_t instances_;
};

class SteerLimiter {
public:
    explicit SteerLimiter(std::int16_t max_rate_per_s);
    std::int16_t limit(std::int16_t request_deci_deg);
    std::int16_t output() const { return output_; }

private:
    std::int16_t max_rate_;
    std::int16_t output_{0};
    std::uint32_t last_ms_{0};
    bool started_{false};
};

class TractionControl {
public:
    explicit TractionControl(svc::IDiagSink* diag = nullptr) : diag_(diag) {}
    std::int16_t torque_limit(const WheelSpeeds& wheels, std::uint16_t vehicle_speed, std::int16_t torque_request,
                              Mode mode);
    bool active() const { return active_; }
    std::uint8_t interventions() const { return interventions_; }

private:
    svc::IDiagSink* diag_;
    MovingAverage<std::int16_t, 8> slip_;
    bool active_{false};
    std::uint8_t interventions_{0};
};

namespace diag {
std::uint16_t code_for(Axle axle, bool left);
const char* name(std::uint16_t code);
}  // namespace diag

inline namespace v2 {
class LevelControl {
public:
    std::int8_t step(std::int16_t height_mm, std::uint16_t speed_kmh, Mode mode, std::uint16_t dt_ms);
    std::int16_t target() const { return target_mm_; }

private:
    std::int16_t target_mm_{0};
    std::uint32_t settle_ms_{0};
    bool lowered_{false};
};
}  // namespace v2

namespace can_io {
bool send_status(const SpeedEstimator& estimator, Mode mode);
bool receive_mode(Mode& mode);
}  // namespace can_io

namespace params {
bool load();
std::int16_t comfort_offset();
std::uint8_t sport_gain();
}  // namespace params

Mode select_mode(Mode current, std::uint8_t button, bool vehicle_stopped);
std::uint16_t pwm_for_axle(Axle axle, std::int16_t demand);
bool pump_step(bool request, std::uint16_t dt_ms);
std::uint16_t supply_derating(std::uint16_t duty);
std::uint8_t pump_faults();

}  // namespace vehicle::chassis

#endif
