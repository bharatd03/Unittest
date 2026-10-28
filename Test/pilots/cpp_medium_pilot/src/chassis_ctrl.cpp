// chassis_ctrl.cpp - chassis control (requirements in docs/requirements.md).
// Calls into several namespaces: hal::adc, hal::pwm (overloads, through an alias), hal::gpio, hal itself
// (a function and a variable), drv::can (a C++17 nested namespace), svc (an injected interface) and a C
// library with C linkage. Its own helpers live in an unnamed namespace.
#include "chassis.hpp"

#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>

#include "drv_can.hpp"
#include "hal_io.hpp"
#include "legacy_nvm.h"

namespace io = hal::pwm;          // a namespace alias
using drv::can::Frame;            // a using-declaration

namespace {

constexpr std::uint16_t kSpreadLimit = 200;        // 20.0 km/h between the fastest and the slowest wheel
constexpr std::uint16_t kSpeedMax = 3000;          // 300.0 km/h
constexpr std::int16_t kRegenFault = -1;
constexpr std::int16_t kRegenStepMax = 500;
constexpr std::uint32_t kMismatchMaxMs = 200000;
constexpr std::uint16_t kDiagRegenInvalid = 0x101;
constexpr std::uint16_t kDiagRegenMismatch = 0x102;
constexpr std::uint8_t kPressureChannel = 3;
constexpr std::uint8_t kLevelPin = 7;
constexpr std::uint8_t kPumpPin = 8;
constexpr std::uint16_t kPumpOnBelow = 1800;
constexpr std::uint16_t kPumpOffAbove = 2200;
constexpr std::uint32_t kPumpMaxOnMs = 300000;
constexpr std::uint32_t kStatusId = 0x2B0;
constexpr std::uint32_t kModeId = 0x2B4;
constexpr std::uint16_t kParamsId = 0x31;
constexpr std::uint8_t kParamsVersion = 3;
constexpr std::uint8_t kPwmFront = 0;
constexpr std::uint8_t kPwmRear = 1;
constexpr std::uint16_t kSupplyOff = 9000;
constexpr std::uint16_t kSupplyHalf = 11000;
constexpr std::int16_t kLevelBand = 5;
constexpr std::uint32_t kLevelSettleMs = 60000;
constexpr std::uint16_t kHighwayKmh = 120;

struct Params {
    std::uint8_t version;
    std::int8_t comfort_offset_mm;
    std::uint8_t sport_gain_pct;
    std::uint8_t crc;
};

Params g_params{kParamsVersion, 10, 120, 0};
bool g_pump_on = false;
std::uint32_t g_pump_on_ms = 0;
std::uint8_t g_pump_faults = 0;
std::uint8_t g_rx_alive = 0;
bool g_rx_alive_valid = false;
std::uint8_t g_tx_alive = 0;

std::int16_t clamp16(std::int32_t value, std::int16_t lo, std::int16_t hi)
{
    if (value < lo) {
        return lo;
    }
    if (value > hi) {
        return hi;
    }
    return static_cast<std::int16_t>(value);
}

std::uint8_t crc8(const std::uint8_t* data, std::size_t len)
{
    std::uint8_t crc = 0;
    for (std::size_t i = 0; i < len; ++i) {
        crc = static_cast<std::uint8_t>(crc ^ data[i]);
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x80u) != 0 ? static_cast<std::uint8_t>((crc << 1) ^ 0x2Fu)
                                     : static_cast<std::uint8_t>(crc << 1);
        }
    }
    return crc;
}

void defaults()
{
    g_params = Params{kParamsVersion, 10, 120, 0};
}

}  // namespace

namespace vehicle::chassis {

bool operator==(const WheelSpeeds& a, const WheelSpeeds& b)
{
    return a.fl == b.fl && a.fr == b.fr && a.rl == b.rl && a.rr == b.rr;
}

// ------------------------------------------------------------------------------------------ moving average
template <typename T, std::size_t N>
T MovingAverage<T, N>::push(T value)
{
    buffer_[next_] = value;
    next_ = (next_ + 1) % N;
    if (count_ < N) {
        ++count_;
    }
    return this->value();
}

template <typename T, std::size_t N>
T MovingAverage<T, N>::value() const
{
    if (count_ == 0) {
        return T{};
    }
    long long sum = 0;
    for (std::size_t i = 0; i < count_; ++i) {
        sum += buffer_[i];
    }
    return static_cast<T>(sum / static_cast<long long>(count_));
}

template class MovingAverage<std::uint16_t, 4>;
template class MovingAverage<std::int16_t, 8>;

// ------------------------------------------------------------------------------------------ speed
std::uint16_t SpeedEstimator::update(const WheelSpeeds& wheels)
{
    std::array<std::uint16_t, 4> sorted{wheels.fl, wheels.fr, wheels.rl, wheels.rr};
    std::sort(sorted.begin(), sorted.end());
    if (sorted[3] > kSpeedMax) {
        implausible_ = std::min<std::uint8_t>(static_cast<std::uint8_t>(implausible_ + 1), 10);
        return speed_;
    }
    if (static_cast<std::uint16_t>(sorted[3] - sorted[0]) > kSpreadLimit) {
        implausible_ = std::min<std::uint8_t>(static_cast<std::uint8_t>(implausible_ + 1), 10);
    } else {
        implausible_ = 0;
    }
    if (!plausible()) {
        return speed_;                       // hold the last estimate while the wheels disagree
    }
    const auto middle = static_cast<std::uint16_t>((sorted[1] + sorted[2]) / 2);
    speed_ = filter_.push(middle);
    return speed_;
}

// ------------------------------------------------------------------------------------------ brake blending
std::uint8_t BrakeBlender::instances_ = 0;

BrakeBlender::BrakeBlender(svc::IDiagSink& diag) : diag_(diag)
{
    ++instances_;
}

std::int16_t BrakeBlender::blend(std::int16_t request, std::int16_t regen_max, bool abs_active, std::uint16_t dt_ms)
{
    std::int16_t regen = 0;

    if (regen_max <= kRegenFault) {
        ++faults_;
        diag_.report(kDiagRegenInvalid, true);
        last_regen_ = 0;
        return request;
    }
    if (request > 0 && !abs_active) {
        regen = std::min(request, regen_max);
        if (diag_.level() >= 2) {
            regen = static_cast<std::int16_t>(regen / 2);
        }
    }
    if (request > 0 && last_regen_ - regen > kRegenStepMax) {
        mismatch_ms_ += dt_ms;
        if (mismatch_ms_ >= kMismatchMaxMs) {
            diag_.report(kDiagRegenMismatch, true);
            mismatch_ms_ = 0;
        }
    } else {
        mismatch_ms_ = 0;
    }
    last_regen_ = regen;
    return clamp16(static_cast<std::int32_t>(request) - regen, 0, std::numeric_limits<std::int16_t>::max());
}

void BrakeBlender::reset()
{
    last_regen_ = 0;
    faults_ = 0;
    mismatch_ms_ = 0;
}

// ------------------------------------------------------------------------------------------ traction
std::int16_t TractionControl::torque_limit(const WheelSpeeds& wheels, std::uint16_t vehicle_speed,
                                           std::int16_t torque_request, Mode mode)
{
    const std::uint16_t driven = std::max(wheels.rl, wheels.rr);
    std::int32_t slip = static_cast<std::int32_t>(driven) - vehicle_speed;
    if (vehicle_speed < 30 && slip < 0) {
        slip = -slip;                        // at walking speed a locked wheel counts as slip too
    }
    const std::int16_t average = slip_.push(clamp16(slip, -1000, 1000));
    const std::int16_t threshold = mode == Mode::Sport ? 80 : 40;
    if (average > threshold && torque_request > 0) {
        if (!active_) {
            ++interventions_;
            if (diag_ != nullptr && interventions_ % 10 == 0) {
                diag_->report(0x201, true);  // frequent interventions: tyres or road worth a note
            }
        }
        active_ = true;
        const std::int32_t cut = std::min<std::int32_t>(average, 100);
        return clamp16(static_cast<std::int32_t>(torque_request) * (100 - cut) / 100, 0, torque_request);
    }
    active_ = false;
    return torque_request;
}

namespace diag {

std::uint16_t code_for(Axle axle, bool left)
{
    if (axle == Axle::Front) {
        return left ? 0x301 : 0x302;
    }
    return left ? 0x303 : 0x304;
}

const char* name(std::uint16_t code)
{
    switch (code) {
    case kDiagRegenInvalid:
        return "regen limit invalid";
    case kDiagRegenMismatch:
        return "regen mismatch";
    case 0x201:
        return "traction interventions";
    case 0x301:
    case 0x302:
        return "front actuator";
    case 0x303:
    case 0x304:
        return "rear actuator";
    default:
        return "unknown";
    }
}

}  // namespace diag

// ------------------------------------------------------------------------------------------ steering
SteerLimiter::SteerLimiter(std::int16_t max_rate_per_s) : max_rate_(max_rate_per_s)
{
    if (max_rate_per_s <= 0) {
        throw std::invalid_argument("the steering rate limit must be positive");
    }
}

std::int16_t SteerLimiter::limit(std::int16_t request_deci_deg)
{
    const std::uint32_t now = hal::now_ms();

    if (!started_) {
        started_ = true;
        last_ms_ = now;
        output_ = request_deci_deg;
        return output_;
    }
    const std::uint32_t dt = now - last_ms_;
    last_ms_ = now;
    if (dt == 0) {
        return output_;
    }
    const std::int32_t step = std::min<std::int32_t>(static_cast<std::int32_t>(max_rate_) * static_cast<std::int32_t>(
                                                         std::min<std::uint32_t>(dt, 10000)) / 1000,
                                                     std::numeric_limits<std::int16_t>::max());
    const std::int32_t delta = static_cast<std::int32_t>(request_deci_deg) - output_;
    if (delta > step) {
        output_ = clamp16(output_ + step, std::numeric_limits<std::int16_t>::min(),
                          std::numeric_limits<std::int16_t>::max());
    } else if (delta < -step) {
        output_ = clamp16(output_ - step, std::numeric_limits<std::int16_t>::min(),
                          std::numeric_limits<std::int16_t>::max());
    } else {
        output_ = request_deci_deg;
    }
    return output_;
}

// ------------------------------------------------------------------------------------------ ride height
inline namespace v2 {

std::int8_t LevelControl::step(std::int16_t height_mm, std::uint16_t speed_kmh, Mode mode, std::uint16_t dt_ms)
{
    if (mode == Mode::Service) {
        settle_ms_ = 0;
        return 0;                            // the workshop holds the height
    }
    if (speed_kmh > kHighwayKmh && (mode == Mode::Sport || mode == Mode::Eco)) {
        target_mm_ = -20;
        lowered_ = true;
    } else if (mode == Mode::Comfort) {
        target_mm_ = params::comfort_offset();
        lowered_ = false;
    } else if (lowered_ && speed_kmh < kHighwayKmh - 20) {
        target_mm_ = 0;
        lowered_ = false;
    }
    const std::int32_t error = static_cast<std::int32_t>(target_mm_) - height_mm;
    if (error >= -kLevelBand && error <= kLevelBand) {
        settle_ms_ = std::min<std::uint32_t>(settle_ms_ + dt_ms, kLevelSettleMs);
        return 0;
    }
    if (settle_ms_ >= kLevelSettleMs && error > -2 * kLevelBand && error < 2 * kLevelBand) {
        return 0;                            // settled: small deviations are not corrected
    }
    settle_ms_ = 0;
    return static_cast<std::int8_t>(error > 0 ? 1 : -1);
}

}  // namespace v2

// ------------------------------------------------------------------------------------------ CAN
namespace can_io {

bool send_status(const SpeedEstimator& estimator, Mode mode)
{
    Frame frame{};
    frame.id = kStatusId;
    frame.len = 8;
    frame.data[0] = static_cast<std::uint8_t>(estimator.speed() & 0xFFu);
    frame.data[1] = static_cast<std::uint8_t>(estimator.speed() >> 8);
    frame.data[2] = static_cast<std::uint8_t>(mode);
    frame.data[3] = estimator.plausible() ? 1u : 0u;
    g_tx_alive = static_cast<std::uint8_t>((g_tx_alive + 1u) & 0x0Fu);
    frame.data[6] = g_tx_alive;
    frame.data[7] = crc8(frame.data, 7);
    return drv::can::send(frame);
}

bool receive_mode(Mode& mode)
{
    Frame frame{};
    if (!drv::can::read(frame)) {
        return false;
    }
    if (frame.id != kModeId || frame.len != 8) {
        return false;
    }
    if (crc8(frame.data, 7) != frame.data[7]) {
        return false;
    }
    const std::uint8_t alive = static_cast<std::uint8_t>(frame.data[6] & 0x0Fu);
    if (g_rx_alive_valid && alive == g_rx_alive) {
        return false;                        // a frame repeated by the sender
    }
    g_rx_alive = alive;
    g_rx_alive_valid = true;
    switch (frame.data[0]) {
    case 0:
        mode = Mode::Comfort;
        return true;
    case 1:
        mode = Mode::Sport;
        return true;
    case 2:
        mode = Mode::Eco;
        return true;
    default:
        return false;                        // Service is never requested over CAN
    }
}

}  // namespace can_io

// ------------------------------------------------------------------------------------------ parameters
namespace params {

bool load()
{
    Params stored{};
    if (nvm_load(kParamsId, &stored, static_cast<std::uint16_t>(sizeof stored)) != 0) {
        defaults();
        return false;
    }
    std::uint8_t bytes[sizeof stored];
    std::memcpy(bytes, &stored, sizeof stored);
    if (stored.version != kParamsVersion || crc8(bytes, sizeof stored - 1) != stored.crc) {
        defaults();
        return false;
    }
    if (stored.comfort_offset_mm < -30 || stored.comfort_offset_mm > 30 || stored.sport_gain_pct < 50 ||
        stored.sport_gain_pct > 200) {
        defaults();
        return false;
    }
    g_params = stored;
    return true;
}

std::int16_t comfort_offset()
{
    return g_params.comfort_offset_mm;
}

std::uint8_t sport_gain()
{
    return g_params.sport_gain_pct;
}

}  // namespace params

// ------------------------------------------------------------------------------------------ modes and actuators
Mode select_mode(Mode current, std::uint8_t button, bool vehicle_stopped)
{
    switch (button) {
    case 1:                                  // short press: the next driving mode
        if (current == Mode::Comfort) {
            return Mode::Sport;
        }
        if (current == Mode::Sport) {
            return Mode::Eco;
        }
        return Mode::Comfort;
    case 2:                                  // long press: the workshop mode, only at standstill
        if (vehicle_stopped && current != Mode::Service) {
            return Mode::Service;
        }
        return current;
    default:
        return current;
    }
}

std::uint16_t supply_derating(std::uint16_t duty)
{
    if (hal::g_supply_mv < kSupplyOff) {
        return 0;
    }
    if (hal::g_supply_mv < kSupplyHalf) {
        return static_cast<std::uint16_t>(duty / 2);
    }
    return duty;
}

std::uint16_t pwm_for_axle(Axle axle, std::int16_t demand)
{
    const std::int32_t scaled = static_cast<std::int32_t>(demand) * params::sport_gain() / 100;
    const auto duty = static_cast<std::uint16_t>(clamp16(scaled, 0, 1000));
    const std::uint16_t derated = supply_derating(duty);
    if (axle == Axle::Front) {
        io::set(kPwmFront, derated);
    } else {
        io::set(kPwmRear, derated, true);    // the rear stage is wired inverted
    }
    return derated;
}

std::uint8_t pump_faults()
{
    return g_pump_faults;
}

bool pump_step(bool request, std::uint16_t dt_ms)
{
    using hal::adc::read;                    // a using-declaration in a block: read is hal::adc::read here
    const std::uint16_t pressure = read(kPressureChannel);
    const bool level_ok = hal::gpio::read(kLevelPin);

    if (!level_ok) {
        ++g_pump_faults;
        g_pump_on = false;
    } else if (request && pressure < kPumpOnBelow && g_pump_on_ms < kPumpMaxOnMs) {
        g_pump_on = true;
    } else if (!request || pressure > kPumpOffAbove) {
        g_pump_on = false;
    }
    if (g_pump_on) {
        g_pump_on_ms += dt_ms;
        if (g_pump_on_ms >= kPumpMaxOnMs) {
            g_pump_on = false;               // thermal protection of the motor
        }
    } else if (g_pump_on_ms > dt_ms) {
        g_pump_on_ms -= dt_ms;               // the motor cools down while off
    } else {
        g_pump_on_ms = 0;
    }
    hal::gpio::write(kPumpPin, g_pump_on);
    return g_pump_on;
}

}  // namespace vehicle::chassis
