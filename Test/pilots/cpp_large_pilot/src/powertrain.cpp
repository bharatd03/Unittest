// powertrain.cpp - engine and transmission control of a passenger car (ASIL parts in docs/requirements.md).
// One translation unit: signal conditioning, torque structure, idle and boost control, injection and ignition,
// knock control, shift logic, cruise control, monitors and CAN signals. Generic code by class and function
// templates, specializations, constexpr tables and CRTP.
#include "powertrain.hpp"
#include "pt_hal.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <numeric>
#include <type_traits>

namespace pt {
namespace detail {

// ------------------------------------------------------------------------------------------ fixed point
template <int Frac>
class Fixed {
public:
    static_assert(Frac > 0 && Frac < 16, "Q format with 1..15 fraction bits");
    constexpr Fixed() = default;
    static constexpr Fixed from_raw(std::int32_t raw)
    {
        Fixed f;
        f.raw_ = raw;
        return f;
    }
    static constexpr Fixed from_int(std::int32_t value) { return from_raw(value * (1 << Frac)); }
    constexpr std::int32_t raw() const { return raw_; }
    constexpr std::int32_t to_int() const { return raw_ >= 0 ? raw_ >> Frac : -((-raw_) >> Frac); }
    constexpr Fixed operator+(Fixed other) const { return from_raw(raw_ + other.raw_); }
    constexpr Fixed operator-(Fixed other) const { return from_raw(raw_ - other.raw_); }
    constexpr Fixed operator*(Fixed other) const
    {
        return from_raw(static_cast<std::int32_t>((static_cast<std::int64_t>(raw_) * other.raw_) >> Frac));
    }
    constexpr Fixed operator/(Fixed other) const
    {
        if (other.raw_ == 0) {
            return from_raw(raw_ >= 0 ? std::numeric_limits<std::int32_t>::max() : std::numeric_limits<std::int32_t>::min());
        }
        return from_raw(static_cast<std::int32_t>((static_cast<std::int64_t>(raw_) * (std::int64_t{1} << Frac)) / other.raw_));
    }
    constexpr bool operator<(Fixed other) const { return raw_ < other.raw_; }
    constexpr bool operator>(Fixed other) const { return raw_ > other.raw_; }
    constexpr bool operator==(Fixed other) const { return raw_ == other.raw_; }

private:
    std::int32_t raw_ = 0;
};

using Q8 = Fixed<8>;
using Q12 = Fixed<12>;

// ------------------------------------------------------------------------------------------ lookup tables
template <typename T, std::size_t N>
class Lut {
public:
    static_assert(N >= 2, "a table needs two points");
    constexpr Lut(const std::array<T, N>& x, const std::array<T, N>& y) : x_(x), y_(y) {}
    constexpr T operator()(T in) const
    {
        if (in <= x_[0]) {
            return y_[0];
        }
        for (std::size_t i = 1; i < N; ++i) {
            if (in <= x_[i]) {
                const auto dx = static_cast<std::int32_t>(x_[i]) - x_[i - 1];
                const auto dy = static_cast<std::int32_t>(y_[i]) - y_[i - 1];
                return static_cast<T>(y_[i - 1] + dy * (static_cast<std::int32_t>(in) - x_[i - 1]) / dx);
            }
        }
        return y_[N - 1];
    }

private:
    std::array<T, N> x_;
    std::array<T, N> y_;
};

template <typename T, std::size_t NX, std::size_t NY>
class Map2d {
public:
    constexpr Map2d(const std::array<T, NX>& x, const std::array<T, NY>& y, const std::array<std::array<T, NY>, NX>& z)
        : x_(x), y_(y), z_(z) {}
    T operator()(T xi, T yi) const
    {
        const std::size_t ix = index(x_, xi);
        const std::size_t iy = index(y_, yi);
        const T z00 = z_[ix][iy];
        const T z10 = z_[ix + 1][iy];
        const T z01 = z_[ix][iy + 1];
        const T z11 = z_[ix + 1][iy + 1];
        const std::int32_t fx = fraction(x_, ix, xi);
        const std::int32_t fy = fraction(y_, iy, yi);
        const std::int32_t a = z00 + (z10 - z00) * fx / 256;
        const std::int32_t b = z01 + (z11 - z01) * fx / 256;
        return static_cast<T>(a + (b - a) * fy / 256);
    }

private:
    template <std::size_t N>
    static std::size_t index(const std::array<T, N>& axis, T v)
    {
        std::size_t i = 0;
        while (i + 2 < N && v > axis[i + 1]) {
            ++i;
        }
        return i;
    }
    template <std::size_t N>
    static std::int32_t fraction(const std::array<T, N>& axis, std::size_t i, T v)
    {
        const T c = clamp(v, axis[i], axis[i + 1]);
        return (static_cast<std::int32_t>(c) - axis[i]) * 256 / (static_cast<std::int32_t>(axis[i + 1]) - axis[i]);
    }
    std::array<T, NX> x_;
    std::array<T, NY> y_;
    std::array<std::array<T, NY>, NX> z_;
};

// ------------------------------------------------------------------------------------------ monitors (CRTP)
template <class Derived>
class Monitor {
public:
    MonitorState update(std::uint16_t dt_ms)
    {
        const bool failed = static_cast<Derived*>(this)->check();
        if (failed) {
            ok_ms_ = 0;
            fail_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(fail_ms_ + dt_ms, 65535u));
            if (fail_ms_ >= Derived::kFailMs) {
                state_ = MonitorState::Failed;
            } else if (state_ == MonitorState::Ok) {
                state_ = MonitorState::Pending;
            }
        } else {
            fail_ms_ = 0;
            ok_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(ok_ms_ + dt_ms, 65535u));
            if (ok_ms_ >= Derived::kHealMs && state_ != MonitorState::Latched) {
                state_ = MonitorState::Ok;
            }
        }
        if (state_ == MonitorState::Failed && Derived::kLatching) {
            state_ = MonitorState::Latched;
        }
        return state_;
    }
    MonitorState state() const { return state_; }
    void reset() { state_ = MonitorState::Ok; fail_ms_ = 0; ok_ms_ = 0; }

private:
    MonitorState state_ = MonitorState::Ok;
    std::uint16_t fail_ms_ = 0;
    std::uint16_t ok_ms_ = 0;
};

// ------------------------------------------------------------------------------------------ CAN signals
template <unsigned Start, unsigned Len, int Factor, int Offset, int Min, int Max>
struct Signal {
    static_assert(Len >= 1 && Len <= 32 && Start + Len <= 64, "the signal lies in the 8 data bytes");
    static std::int32_t decode(const std::array<std::uint8_t, 8>& data)
    {
        std::uint32_t raw = 0;
        for (unsigned i = 0; i < Len; ++i) {
            const unsigned bit = Start + i;
            if ((data[bit / 8] >> (bit % 8)) & 1u) {
                raw |= (1u << i);
            }
        }
        return static_cast<std::int32_t>(raw) * Factor + Offset;
    }
    static bool encode(std::array<std::uint8_t, 8>& data, std::int32_t physical)
    {
        if (physical < Min || physical > Max) {
            return false;
        }
        const auto raw = static_cast<std::uint32_t>((physical - Offset) / Factor);
        for (unsigned i = 0; i < Len; ++i) {
            const unsigned bit = Start + i;
            const auto mask = static_cast<std::uint8_t>(1u << (bit % 8));
            if ((raw >> i) & 1u) {
                data[bit / 8] = static_cast<std::uint8_t>(data[bit / 8] | mask);
            } else {
                data[bit / 8] = static_cast<std::uint8_t>(data[bit / 8] & ~mask);
            }
        }
        return true;
    }
};

}  // namespace detail

namespace {
using detail::clamp;
using detail::abs_value;

// ------------------------------------------------------------------------------------------ tables
constexpr detail::Lut<std::int16_t, 6> kIdleTarget({-30, -10, 0, 20, 60, 90}, {1300, 1150, 1050, 900, 800, 750});
constexpr detail::Lut<std::int16_t, 5> kColdEnrichment({-30, -10, 10, 40, 80}, {180, 150, 125, 110, 100});
constexpr detail::Lut<std::int16_t, 5> kFanDuty({85, 90, 95, 100, 105}, {0, 20, 45, 75, 100});
constexpr detail::Lut<std::int16_t, 5> kTempDerate({100, 105, 110, 115, 120}, {100, 95, 85, 70, 50});
constexpr detail::Lut<std::int16_t, 4> kKnockRecovery({0, 2000, 4000, 6000}, {20, 15, 10, 5});
constexpr detail::Lut<std::int16_t, 5> kBoostTarget({1500, 2000, 3000, 4500, 6000}, {0, 400, 900, 1000, 800});
constexpr detail::Lut<std::int16_t, 4> kHeaterDuty({-20, 0, 300, 600}, {100, 80, 40, 0});
constexpr detail::Lut<std::int16_t, 6> kGearMaxTorque({1, 2, 3, 4, 5, 6}, {250, 320, 380, 400, 400, 400});

const detail::Map2d<std::int16_t, 5, 4> kPedalMap(
    {0, 25, 50, 75, 100}, {800, 2000, 4000, 6000},
    {{{{0, 0, 0, 0}}, {{90, 80, 70, 60}}, {{190, 180, 170, 150}}, {{300, 290, 280, 260}}, {{400, 400, 390, 360}}}});
const detail::Map2d<std::int16_t, 4, 4> kAdvanceMap(
    {800, 2000, 4000, 6000}, {10, 40, 70, 100},
    {{{{120, 110, 90, 70}}, {{240, 220, 180, 140}}, {{320, 300, 250, 200}}, {{340, 320, 270, 220}}}});
}  // namespace

std::int16_t idle_target_rpm(std::int16_t coolant_c)
{
    return kIdleTarget(coolant_c);
}

std::int16_t cold_enrichment(std::int16_t coolant_c)
{
    return kColdEnrichment(coolant_c);
}

std::int16_t temperature_derate(std::int16_t oil_c)
{
    return kTempDerate(oil_c);
}

std::int16_t pedal_torque(std::int16_t pedal_pct, std::int16_t rpm)
{
    return kPedalMap(detail::clamp<std::int16_t>(pedal_pct, 0, 100), rpm);
}

std::int16_t base_advance(std::int16_t rpm, std::int16_t load_pct)
{
    return kAdvanceMap(rpm, load_pct);
}

std::int32_t q8_multiply(std::int32_t a_raw, std::int32_t b_raw)
{
    return (detail::Q8::from_raw(a_raw) * detail::Q8::from_raw(b_raw)).raw();
}

std::int32_t q12_divide(std::int32_t a_raw, std::int32_t b_raw)
{
    return (detail::Q12::from_raw(a_raw) / detail::Q12::from_raw(b_raw)).raw();
}

std::int32_t q8_to_int(std::int32_t raw)
{
    return detail::Q8::from_raw(raw).to_int();
}

std::int16_t saturate_i16(std::int32_t wide)
{
    return detail::saturate<std::int16_t>(wide);
}

std::uint16_t saturate_u16(std::int32_t wide)
{
    return detail::saturate<std::uint16_t>(wide);
}

bool all_ready(bool a, bool b, bool c, bool d)
{
    return detail::all_of(a, b, c, d);
}

bool any_request(bool a, bool b, bool c)
{
    return detail::any_of(a, b, c);
}

// ------------------------------------------------------------------------------------------ engine speed
EngineSpeed::EngineSpeed(std::uint8_t teeth, std::uint8_t missing) : teeth_(teeth == 0 ? 60 : teeth), missing_(missing) {}

SyncState EngineSpeed::tooth(std::uint32_t period_us)
{
    if (period_us == 0 || period_us > kStallPeriodUs) {
        sync_ = SyncState::Lost;
        count_ = 0;
        rpm_ = 0;
        last_period_ = 0;
        return sync_;
    }
    const bool gap = last_period_ != 0 && period_us > last_period_ * 2u && period_us < last_period_ * 4u;
    if (gap) {
        if (sync_ == SyncState::Synced && count_ != static_cast<std::uint16_t>(teeth_ - missing_ - 1)) {
            sync_ = SyncState::Lost;
            ++sync_errors_;
        } else {
            sync_ = SyncState::Synced;
        }
        count_ = 0;
    } else {
        ++count_;
        if (sync_ == SyncState::Lost && count_ > teeth_) {
            sync_ = SyncState::Searching;
        }
        const std::uint32_t rev_us = period_us * teeth_;
        rpm_ = static_cast<std::int16_t>(std::min<std::uint32_t>(60000000u / rev_us, 16000u));
        filtered_ = rpm_filter_.update(rpm_);
    }
    last_period_ = period_us;
    return sync_;
}

std::int16_t EngineSpeed::rpm() const
{
    return rpm_;
}

std::int16_t EngineSpeed::filtered_rpm() const
{
    return filtered_;
}

bool EngineSpeed::overspeed(std::int16_t limit) const
{
    return sync_ == SyncState::Synced && filtered_ > limit;
}

std::uint16_t EngineSpeed::sync_errors() const
{
    return sync_errors_;
}

void EngineSpeed::reset()
{
    sync_ = SyncState::Lost;
    count_ = 0;
    rpm_ = 0;
    filtered_ = 0;
    last_period_ = 0;
    sync_errors_ = 0;
    rpm_filter_.reset();
}

// ------------------------------------------------------------------------------------------ pedal
namespace {
class PedalPlausibility : public detail::Monitor<PedalPlausibility> {
public:
    static constexpr std::uint16_t kFailMs = 100;
    static constexpr std::uint16_t kHealMs = 500;
    static constexpr bool kLatching = true;
    void set(std::int16_t track1, std::int16_t track2) { t1_ = track1; t2_ = track2; }
    bool check() const
    {
        const std::int16_t expected2 = static_cast<std::int16_t>(t1_ / 2);
        return detail::abs_value<std::int16_t>(static_cast<std::int16_t>(expected2 - t2_)) > kPedalTolerance ||
               t1_ < kPedalMinRaw || t1_ > kPedalMaxRaw;
    }

private:
    std::int16_t t1_ = 0;
    std::int16_t t2_ = 0;
};

PedalPlausibility g_pedal_monitor;
detail::Ema<std::int16_t, 2> g_pedal_filter;
}  // namespace

PedalResult pedal_position(std::int16_t track1_raw, std::int16_t track2_raw, bool brake, std::uint16_t dt_ms)
{
    PedalResult r{};
    g_pedal_monitor.set(track1_raw, track2_raw);
    r.monitor = g_pedal_monitor.update(dt_ms);
    if (r.monitor == MonitorState::Failed || r.monitor == MonitorState::Latched) {
        r.percent = 0;
        r.limp_home = true;
        return r;
    }
    const std::int32_t span = kPedalMaxRaw - kPedalMinRaw;
    std::int32_t pct = (static_cast<std::int32_t>(track1_raw) - kPedalMinRaw) * 100 / span;
    pct = detail::clamp<std::int32_t>(pct, 0, 100);
    r.percent = g_pedal_filter.update(static_cast<std::int16_t>(pct));
    if (brake && r.percent > kBrakeOverridePct) {
        r.brake_override = true;
        r.percent = 0;
    }
    return r;
}

void pedal_reset()
{
    g_pedal_monitor.reset();
    g_pedal_filter.reset();
}

// ------------------------------------------------------------------------------------------ torque structure
TorqueArbiter::TorqueArbiter(std::int16_t max_torque) : max_torque_(max_torque > 0 ? max_torque : 1), shaper_(15, 40) {}

std::int16_t TorqueArbiter::arbitrate(const TorqueRequests& req)
{
    std::int16_t target = req.driver;
    source_ = TorqueSource::Driver;
    if (req.cruise_active && req.cruise > target) {
        target = req.cruise;
        source_ = TorqueSource::Cruise;
    }
    if (req.speed_limit_active && target > req.speed_limit) {
        target = req.speed_limit;
        source_ = TorqueSource::SpeedLimiter;
    }
    const std::int16_t gear_max = req.gear >= 1 && req.gear <= 6 ? kGearMaxTorque(req.gear) : max_torque_;
    const std::int16_t derated = static_cast<std::int16_t>(std::min<std::int32_t>(gear_max, max_torque_) *
                                                           temperature_derate(req.oil_temp) / 100);
    if (target > derated) {
        target = derated;
        source_ = TorqueSource::Limit;
    }
    if (req.stability_reduce && target > req.stability) {
        target = req.stability;
        source_ = TorqueSource::Stability;
    }
    if (req.shift_intervention) {
        target = static_cast<std::int16_t>(target / 2);
        source_ = TorqueSource::Shift;
    }
    if (target < req.minimum) {
        target = req.minimum;
        source_ = TorqueSource::Minimum;
    }
    if (source_ == TorqueSource::Stability || source_ == TorqueSource::Shift) {
        shaper_.set(target);
        return target;
    }
    return shaper_.step(target);
}

TorqueSource TorqueArbiter::source() const
{
    return source_;
}

void TorqueArbiter::reset()
{
    source_ = TorqueSource::Driver;
    shaper_.set(0);
}

// ------------------------------------------------------------------------------------------ idle speed control
IdleController::IdleController() : pid_(96, 4, 32, 0, 1000) {}

std::int16_t IdleController::step(const IdleInputs& in)
{
    if (!in.engine_running || in.pedal_pct > kIdlePedalPct || in.vehicle_kph > kIdleMaxKph) {
        active_ = false;
        pid_.reset();
        return 0;
    }
    active_ = true;
    std::int16_t target = idle_target_rpm(in.coolant_c);
    std::int16_t feed_forward = 0;
    if (in.ac_on) {
        target = static_cast<std::int16_t>(target + 50);
        feed_forward = static_cast<std::int16_t>(feed_forward + 80);
    }
    if (in.steering_load) {
        feed_forward = static_cast<std::int16_t>(feed_forward + 60);
    }
    if (in.battery_mv < kIdleLowBatteryMv) {
        target = static_cast<std::int16_t>(target + 100);
    }
    target_ = target;
    const std::int16_t correction = pid_.step(target, in.rpm, in.rpm < kStallRpm);
    return detail::clamp<std::int16_t>(static_cast<std::int16_t>(correction + feed_forward), 0, 1000);
}

std::int16_t IdleController::target() const
{
    return target_;
}

bool IdleController::active() const
{
    return active_;
}

void IdleController::reset()
{
    pid_.reset();
    active_ = false;
    target_ = 0;
}

// ------------------------------------------------------------------------------------------ boost control
BoostController::BoostController() : pid_(160, 6, 0, 0, 1000) {}

std::int16_t BoostController::step(std::int16_t rpm, std::int16_t boost_hpa, std::int16_t intake_c, bool knock_active,
                                   std::uint16_t dt_ms)
{
    std::int16_t target = kBoostTarget(rpm);
    if (intake_c > kIntakeHotC) {
        target = static_cast<std::int16_t>(target * 80 / 100);
    }
    if (knock_active) {
        target = static_cast<std::int16_t>(target - 150);
    }
    target = detail::clamp<std::int16_t>(target, 0, kBoostMaxHpa);
    if (boost_hpa > target + kOverboostHpa) {
        overboost_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(overboost_ms_ + dt_ms, 60000u));
    } else {
        overboost_ms_ = 0;
    }
    if (overboost_ms_ >= kOverboostMs) {
        fault_ = true;
    }
    if (fault_) {
        pid_.reset();
        return 0;
    }
    return pid_.step(target, boost_hpa, false);
}

bool BoostController::fault() const
{
    return fault_;
}

void BoostController::reset()
{
    pid_.reset();
    overboost_ms_ = 0;
    fault_ = false;
}

// ------------------------------------------------------------------------------------------ injection
InjectionResult injection(const InjectionInputs& in)
{
    InjectionResult r{};
    if (in.rpm > kRevLimitRpm) {
        r.cut = CutReason::Overspeed;
        return r;
    }
    if (in.pedal_pct == 0 && in.rpm > kOverrunCutRpm && in.coolant_c > kOverrunMinCoolant) {
        r.cut = CutReason::Overrun;
        return r;
    }
    if (in.crash) {
        r.cut = CutReason::Crash;
        return r;
    }
    std::int32_t pulse = static_cast<std::int32_t>(in.airmass_mg) * kInjectorUsPerMg / 10;
    pulse = pulse * cold_enrichment(in.coolant_c) / 100;
    std::int32_t lambda = in.lambda_closed_loop ? detail::clamp<std::int32_t>(in.lambda_correction, -250, 250) : 0;
    pulse = pulse * (1000 + lambda) / 1000;
    if (in.battery_mv < 12000) {
        pulse += (12000 - in.battery_mv) / 10;
    }
    r.pulse_us = static_cast<std::uint16_t>(detail::clamp<std::int32_t>(pulse, 0, kMaxPulseUs));
    r.cut = CutReason::None;
    r.saturated = pulse >= kMaxPulseUs;
    return r;
}

// ------------------------------------------------------------------------------------------ knock control
KnockControl::KnockControl() = default;

std::int16_t KnockControl::cylinder(std::uint8_t cyl, std::uint16_t intensity, std::uint16_t threshold, std::int16_t rpm)
{
    if (cyl >= kCylinders) {
        return 0;
    }
    std::int16_t& retard = retard_[cyl];
    if (intensity > threshold) {
        retard = static_cast<std::int16_t>(std::min<std::int32_t>(retard + kKnockStep, kKnockMaxRetard));
        ++events_[cyl];
        recover_[cyl] = 0;
    } else if (retard > 0) {
        recover_[cyl] = static_cast<std::uint16_t>(recover_[cyl] + 1);
        if (recover_[cyl] >= static_cast<std::uint16_t>(kKnockRecovery(rpm))) {
            retard = static_cast<std::int16_t>(retard - 1);
            recover_[cyl] = 0;
        }
    }
    return retard;
}

std::int16_t KnockControl::max_retard() const
{
    return *std::max_element(retard_.begin(), retard_.end());
}

std::uint32_t KnockControl::events(std::uint8_t cyl) const
{
    return cyl < kCylinders ? events_[cyl] : 0u;
}

bool KnockControl::active() const
{
    return std::any_of(retard_.begin(), retard_.end(), [](std::int16_t r) { return r > 0; });
}

void KnockControl::reset()
{
    retard_.fill(0);
    recover_.fill(0);
    events_.fill(0);
}

// ------------------------------------------------------------------------------------------ ignition
std::int16_t ignition_advance(std::int16_t rpm, std::int16_t load_pct, std::int16_t coolant_c, std::int16_t knock_retard,
                              bool idle_active, std::int16_t idle_error_rpm)
{
    std::int32_t advance = base_advance(rpm, load_pct);
    if (coolant_c < 20) {
        advance += 40;
    } else if (coolant_c > 105) {
        advance -= (coolant_c - 105) * 5;
    }
    advance -= knock_retard * 10;
    if (idle_active) {
        advance += detail::clamp<std::int32_t>(idle_error_rpm / 5, -50, 50);
    }
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(advance, kMinAdvance, kMaxAdvance));
}

// ------------------------------------------------------------------------------------------ shift schedule
ShiftSchedule::ShiftSchedule(DriveMode mode) : mode_(mode) {}

namespace {
constexpr std::array<std::int16_t, 6> kUpEco{0, 18, 32, 48, 62, 80};
constexpr std::array<std::int16_t, 6> kUpSport{0, 28, 48, 70, 92, 115};
constexpr std::array<std::int16_t, 6> kDownEco{0, 0, 12, 24, 38, 52};
constexpr std::array<std::int16_t, 6> kDownSport{0, 0, 20, 38, 58, 78};

std::int16_t shift_point(const std::array<std::int16_t, 6>& table, std::int16_t gear, std::int16_t pedal_pct)
{
    const std::int16_t base = table[static_cast<std::size_t>(gear - 1)];
    return static_cast<std::int16_t>(base + base * pedal_pct / 200);
}
}  // namespace

std::int16_t ShiftSchedule::select(std::int16_t speed_kph, std::int16_t pedal_pct, bool kickdown, std::int16_t manual_request)
{
    if (mode_ == DriveMode::Manual) {
        if (manual_request > 0 && gear_ < 6) {
            ++gear_;
        } else if (manual_request < 0 && gear_ > 1) {
            const std::int16_t down_speed = kUpSport[static_cast<std::size_t>(gear_ - 1)];
            if (speed_kph < down_speed + 40) {
                --gear_;
            }
        }
        return gear_;
    }
    const auto& up = mode_ == DriveMode::Sport ? kUpSport : kUpEco;
    const auto& down = mode_ == DriveMode::Sport ? kDownSport : kDownEco;
    if (kickdown && gear_ > 2 && speed_kph < shift_point(up, gear_, 100)) {
        gear_ = static_cast<std::int16_t>(gear_ - 2);
    } else if (gear_ < 6 && speed_kph > shift_point(up, static_cast<std::int16_t>(gear_ + 1), pedal_pct)) {
        ++gear_;
    } else if (gear_ > 1 && speed_kph < shift_point(down, gear_, pedal_pct)) {
        --gear_;
    }
    return gear_;
}

void ShiftSchedule::set_mode(DriveMode mode)
{
    mode_ = mode;
}

std::int16_t ShiftSchedule::gear() const
{
    return gear_;
}

void ShiftSchedule::reset()
{
    gear_ = 1;
}

// ------------------------------------------------------------------------------------------ converter lockup
LockupState converter_lockup(std::int16_t gear, std::int16_t speed_kph, std::int16_t slip_rpm, std::int16_t pedal_pct,
                             std::int16_t oil_c, bool shifting)
{
    if (gear < 3 || shifting || oil_c < kLockupMinOilC) {
        return LockupState::Open;
    }
    if (pedal_pct > 85 && speed_kph < 80) {
        return LockupState::Open;
    }
    if (speed_kph > kLockupKph && slip_rpm < kLockupSlipRpm) {
        return LockupState::Locked;
    }
    if (speed_kph > kLockupKph - 10) {
        return LockupState::Slipping;
    }
    return LockupState::Open;
}

// ------------------------------------------------------------------------------------------ cruise control
CruiseState CruiseControl::step(const CruiseInputs& in)
{
    if (in.brake || in.clutch || in.stability_active || in.speed_kph < kCruiseMinKph) {
        if (state_ == CruiseState::Active || state_ == CruiseState::Override) {
            state_ = CruiseState::Standby;
        }
        if (in.stability_active) {
            ++cancels_;
        }
        return state_;
    }
    switch (state_) {
    case CruiseState::Off:
        if (in.main_switch) {
            state_ = CruiseState::Standby;
        }
        break;
    case CruiseState::Standby:
        if (!in.main_switch) {
            state_ = CruiseState::Off;
            set_kph_ = 0;
        } else if (in.set) {
            set_kph_ = detail::clamp<std::int16_t>(in.speed_kph, kCruiseMinKph, kCruiseMaxKph);
            state_ = CruiseState::Active;
        } else if (in.resume && set_kph_ > 0) {
            state_ = CruiseState::Active;
        }
        break;
    case CruiseState::Active:
        if (!in.main_switch) {
            state_ = CruiseState::Off;
            set_kph_ = 0;
        } else if (in.cancel) {
            state_ = CruiseState::Standby;
        } else if (in.pedal_pct > kCruiseOverridePct) {
            state_ = CruiseState::Override;
        } else if (in.accel) {
            set_kph_ = static_cast<std::int16_t>(std::min<std::int32_t>(set_kph_ + kCruiseStep, kCruiseMaxKph));
        } else if (in.decel) {
            set_kph_ = static_cast<std::int16_t>(std::max<std::int32_t>(set_kph_ - kCruiseStep, kCruiseMinKph));
        }
        break;
    case CruiseState::Override:
        if (in.pedal_pct <= kCruiseOverridePct) {
            state_ = CruiseState::Active;
        } else if (in.cancel) {
            state_ = CruiseState::Standby;
        }
        break;
    default:
        state_ = CruiseState::Off;
        break;
    }
    return state_;
}

std::int16_t CruiseControl::torque_request(std::int16_t speed_kph, std::int16_t accel_cms2) const
{
    if (state_ != CruiseState::Active) {
        return 0;
    }
    const std::int32_t error = set_kph_ - speed_kph;
    const std::int32_t request = 60 + error * 12 - accel_cms2 / 20;
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(request, 0, 250));
}

std::int16_t CruiseControl::set_speed() const
{
    return set_kph_;
}

std::uint16_t CruiseControl::cancels() const
{
    return cancels_;
}

void CruiseControl::reset()
{
    state_ = CruiseState::Off;
    set_kph_ = 0;
    cancels_ = 0;
}

// ------------------------------------------------------------------------------------------ cooling and after-run
std::uint8_t CoolingControl::fan(std::int16_t coolant_c, bool ac_on, bool engine_running, std::uint16_t dt_ms)
{
    std::int16_t duty = kFanDuty(coolant_c);
    if (ac_on && duty < 30) {
        duty = 30;
    }
    if (!engine_running) {
        if (was_running_ && coolant_c > kAfterRunC) {
            after_run_ms_ = kAfterRunMs;
        }
        was_running_ = false;
        if (after_run_ms_ > 0) {
            after_run_ms_ = after_run_ms_ > dt_ms ? static_cast<std::uint16_t>(after_run_ms_ - dt_ms) : 0;
            return 60;
        }
        return 0;
    }
    was_running_ = true;
    after_run_ms_ = 0;
    hal::pwm(hal::kPwmFan, static_cast<std::uint16_t>(duty));
    return static_cast<std::uint8_t>(duty);
}

bool CoolingControl::thermostat(std::int16_t coolant_c, std::int16_t load_pct) const
{
    const std::int16_t open_at = load_pct > 70 ? 85 : 95;
    return coolant_c >= open_at;
}

void CoolingControl::reset()
{
    after_run_ms_ = 0;
    was_running_ = false;
}

// ------------------------------------------------------------------------------------------ fuel pressure
FuelPump::FuelPump() : pid_(200, 8, 16, 0, 100) {}

std::uint8_t FuelPump::step(std::int16_t rail_bar_x10, std::int16_t demand_mg, bool engine_running, bool crash)
{
    if (crash || (!engine_running && prime_ms_ >= kPrimeMs)) {
        pid_.reset();
        return 0;
    }
    if (!engine_running) {
        prime_ms_ = static_cast<std::uint16_t>(prime_ms_ + 10);
        return 100;
    }
    const std::int16_t target = static_cast<std::int16_t>(detail::clamp<std::int32_t>(50 + demand_mg / 4, 50, 200));
    return static_cast<std::uint8_t>(pid_.step(target, rail_bar_x10, false));
}

void FuelPump::reset()
{
    pid_.reset();
    prime_ms_ = 0;
}

// ------------------------------------------------------------------------------------------ CoolantSensorMonitor
namespace {
class CoolantSensorMonitorImpl : public detail::Monitor<CoolantSensorMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 500;
    static constexpr std::uint16_t kHealMs = 2000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 50 || raw_ > 4000; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
CoolantSensorMonitorImpl g_CoolantSensorMonitor;
}  // namespace

MonitorState monitor_CoolantSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_CoolantSensorMonitor.set(value, reference);
    return g_CoolantSensorMonitor.update(dt_ms);
}

void monitor_CoolantSensorMonitor_reset()
{
    g_CoolantSensorMonitor.reset();
}

// ------------------------------------------------------------------------------------------ MafPlausibility
namespace {
class MafPlausibilityImpl : public detail::Monitor<MafPlausibilityImpl> {
public:
    static constexpr std::uint16_t kFailMs = 300;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ > 1200 && rpm_ < 1000; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
MafPlausibilityImpl g_MafPlausibility;
}  // namespace

MonitorState monitor_MafPlausibility(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_MafPlausibility.set(value, reference);
    return g_MafPlausibility.update(dt_ms);
}

void monitor_MafPlausibility_reset()
{
    g_MafPlausibility.reset();
}

// ------------------------------------------------------------------------------------------ ThrottleTracking
namespace {
class ThrottleTrackingImpl : public detail::Monitor<ThrottleTrackingImpl> {
public:
    static constexpr std::uint16_t kFailMs = 200;
    static constexpr std::uint16_t kHealMs = 500;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return detail::abs_value<std::int16_t>(static_cast<std::int16_t>(raw_ - rpm_)) > 80; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
ThrottleTrackingImpl g_ThrottleTracking;
}  // namespace

MonitorState monitor_ThrottleTracking(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_ThrottleTracking.set(value, reference);
    return g_ThrottleTracking.update(dt_ms);
}

void monitor_ThrottleTracking_reset()
{
    g_ThrottleTracking.reset();
}

// ------------------------------------------------------------------------------------------ SpeedSensorMonitor
namespace {
class SpeedSensorMonitorImpl : public detail::Monitor<SpeedSensorMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 2000;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ == 0 && rpm_ > 3000; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
SpeedSensorMonitorImpl g_SpeedSensorMonitor;
}  // namespace

MonitorState monitor_SpeedSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_SpeedSensorMonitor.set(value, reference);
    return g_SpeedSensorMonitor.update(dt_ms);
}

void monitor_SpeedSensorMonitor_reset()
{
    g_SpeedSensorMonitor.reset();
}

// ------------------------------------------------------------------------------------------ BrakeSwitchMonitor
namespace {
class BrakeSwitchMonitorImpl : public detail::Monitor<BrakeSwitchMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 1000;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return (raw_ != 0) != (rpm_ != 0); }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
BrakeSwitchMonitorImpl g_BrakeSwitchMonitor;
}  // namespace

MonitorState monitor_BrakeSwitchMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_BrakeSwitchMonitor.set(value, reference);
    return g_BrakeSwitchMonitor.update(dt_ms);
}

void monitor_BrakeSwitchMonitor_reset()
{
    g_BrakeSwitchMonitor.reset();
}

// ------------------------------------------------------------------------------------------ OilPressureMonitor
namespace {
class OilPressureMonitorImpl : public detail::Monitor<OilPressureMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 1000;
    static constexpr std::uint16_t kHealMs = 3000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 80 && rpm_ > 1500; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
OilPressureMonitorImpl g_OilPressureMonitor;
}  // namespace

MonitorState monitor_OilPressureMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_OilPressureMonitor.set(value, reference);
    return g_OilPressureMonitor.update(dt_ms);
}

void monitor_OilPressureMonitor_reset()
{
    g_OilPressureMonitor.reset();
}

// ------------------------------------------------------------------------------------------ misfire counting
MisfireResult MisfireCounter::revolution(const std::array<std::int16_t, kCylinders>& accel)
{
    MisfireResult r{};
    const auto mean = std::accumulate(accel.begin(), accel.end(), std::int32_t{0}) / static_cast<std::int32_t>(kCylinders);
    for (std::uint8_t c = 0; c < kCylinders; ++c) {
        if (accel[c] < mean - kMisfireDrop) {
            ++misfires_[c];
            r.misfire_now = true;
        }
    }
    ++revolutions_;
    if (revolutions_ >= kMisfireWindow) {
        const auto worst = std::max_element(misfires_.begin(), misfires_.end());
        r.window_done = true;
        r.worst_cylinder = static_cast<std::uint8_t>(worst - misfires_.begin());
        r.catalyst_damage = *worst * 100u > kMisfireWindow * kCatalystDamagePct;
        r.emission_relevant = std::count_if(misfires_.begin(), misfires_.end(),
                                            [](std::uint16_t m) { return m * 100u > kMisfireWindow * kEmissionPct; }) > 0;
        misfires_.fill(0);
        revolutions_ = 0;
    }
    return r;
}

void MisfireCounter::reset()
{
    misfires_.fill(0);
    revolutions_ = 0;
}

// ------------------------------------------------------------------------------------------ CAN signals
namespace {
using EngineSpeedSig = detail::Signal<0, 16, 1, 0, 0, 16000>;
using CoolantSig = detail::Signal<16, 8, 1, -40, -40, 215>;
using TorqueSig = detail::Signal<24, 12, 1, -500, -500, 1500>;
using GearSig = detail::Signal<36, 4, 1, 0, 0, 7>;
using PedalSig = detail::Signal<40, 8, 1, 0, 0, 100>;
using CruiseSig = detail::Signal<48, 8, 1, 0, 0, 250>;
using MilSig = detail::Signal<56, 1, 1, 0, 0, 1>;
using AliveSig = detail::Signal<60, 4, 1, 0, 0, 15>;

std::uint8_t crc8(const std::array<std::uint8_t, 8>& data, std::size_t from)
{
    std::uint8_t crc = 0xFF;
    for (std::size_t i = from; i < data.size(); ++i) {
        crc = static_cast<std::uint8_t>(crc ^ data[i]);
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x80u) != 0 ? static_cast<std::uint8_t>((crc << 1) ^ 0x2Fu) : static_cast<std::uint8_t>(crc << 1);
        }
    }
    return static_cast<std::uint8_t>(~crc);
}
}  // namespace

bool pack_engine_status(const EngineStatus& s, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return EngineSpeedSig::encode(frame, s.rpm) && CoolantSig::encode(frame, s.coolant_c) &&
           TorqueSig::encode(frame, s.torque_nm) && GearSig::encode(frame, s.gear) && PedalSig::encode(frame, s.pedal_pct) &&
           CruiseSig::encode(frame, s.cruise_kph) && MilSig::encode(frame, s.mil ? 1 : 0) &&
           AliveSig::encode(frame, s.alive & 0x0F);
}

EngineStatus unpack_engine_status(const std::array<std::uint8_t, 8>& frame)
{
    EngineStatus s{};
    s.rpm = EngineSpeedSig::decode(frame);
    s.coolant_c = CoolantSig::decode(frame);
    s.torque_nm = TorqueSig::decode(frame);
    s.gear = GearSig::decode(frame);
    s.pedal_pct = PedalSig::decode(frame);
    s.cruise_kph = CruiseSig::decode(frame);
    s.mil = MilSig::decode(frame) != 0;
    s.alive = AliveSig::decode(frame);
    return s;
}

std::uint8_t frame_crc(const std::array<std::uint8_t, 8>& frame)
{
    return crc8(frame, 1);
}

bool AliveCheck::frame(std::uint8_t counter)
{
    counter = static_cast<std::uint8_t>(counter & 0x0F);
    if (!started_) {
        started_ = true;
        last_ = counter;
        return true;
    }
    const std::uint8_t delta = static_cast<std::uint8_t>((counter - last_) & 0x0F);
    last_ = counter;
    if (delta == 0 || delta > kAliveMaxDelta) {
        errors_ = static_cast<std::uint8_t>(std::min(errors_ + 1, 255));
        return errors_ < kAliveMaxErrors;
    }
    if (errors_ > 0) {
        --errors_;
    }
    return true;
}

std::uint8_t AliveCheck::errors() const
{
    return errors_;
}

void AliveCheck::reset()
{
    started_ = false;
    last_ = 0;
    errors_ = 0;
}

// ------------------------------------------------------------------------------------------ event log
bool EventLog::record(EventCode code, std::uint32_t time_ms, std::int16_t value)
{
    return events_.push(Event{code, time_ms, value});
}

std::size_t EventLog::count(EventCode code) const
{
    return events_.count_if([code](const Event& e) { return e.code == code; });
}

std::size_t EventLog::count_above(std::int16_t value) const
{
    return events_.count_if([value](const Event& e) { return e.value > value; });
}

bool EventLog::oldest(Event& out)
{
    return events_.pop(out);
}

std::size_t EventLog::size() const
{
    return events_.size();
}

std::size_t EventLog::overflows() const
{
    return events_.overflows();
}

void EventLog::clear()
{
    events_.clear();
}

// ------------------------------------------------------------------------------------------ electronic throttle
ThrottleBody::ThrottleBody() : pid_(220, 10, 40, -100, 100) {}

std::int16_t ThrottleBody::step(std::int16_t target_pct, std::int16_t position_pct, bool monitor_failed)
{
    if (monitor_failed || limp_) {
        limp_ = true;
        pid_.reset();
        return 0;
    }
    const std::int16_t target = detail::clamp<std::int16_t>(target_pct, kThrottleMinPct, 100);
    const std::int16_t duty = pid_.step(target, position_pct, false);
    hal::pwm(hal::kPwmThrottle, static_cast<std::uint16_t>(duty + 100));
    return duty;
}

bool ThrottleBody::limp_home() const
{
    return limp_;
}

void ThrottleBody::reset()
{
    pid_.reset();
    limp_ = false;
}

// ------------------------------------------------------------------------------------------ start/stop
StopStart::Decision StopStart::step(const StopStartInputs& in, std::uint16_t dt_ms)
{
    const bool conditions = detail::all_of(in.enabled, in.battery_ok, in.coolant_c > 60, in.coolant_c < 105,
                                           !in.ac_max, in.driver_belt, in.bonnet_closed);
    if (!in.engine_running) {
        stopped_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(stopped_ms_ + dt_ms, 65535u));
        if (detail::any_of(!in.brake, in.steering, !in.battery_ok, stopped_ms_ > kMaxStopMs)) {
            stopped_ms_ = 0;
            return Decision::Restart;
        }
        return Decision::Hold;
    }
    stopped_ms_ = 0;
    if (in.speed_kph < 1) {
        standstill_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(standstill_ms_ + dt_ms, 65535u));
    } else {
        standstill_ms_ = 0;
        moved_ = in.speed_kph > 8 || moved_;
    }
    if (conditions && in.brake && moved_ && standstill_ms_ >= kStopDelayMs) {
        moved_ = false;
        return Decision::Stop;
    }
    return Decision::Hold;
}

void StopStart::reset()
{
    stopped_ms_ = 0;
    standstill_ms_ = 0;
    moved_ = false;
}

// ------------------------------------------------------------------------------------------ lambda heater
std::uint8_t lambda_heater(std::int16_t sensor_c, std::int16_t exhaust_c, std::uint32_t since_start_ms)
{
    if (since_start_ms < kHeaterDelayMs && exhaust_c < 100) {
        return 0;
    }
    if (sensor_c > 850) {
        return 0;
    }
    return static_cast<std::uint8_t>(kHeaterDuty(sensor_c));
}

// ------------------------------------------------------------------------------------------ sensor hub (injected bus)
SensorHub::SensorHub(ISensorBus& bus) : bus_(bus) {}

bool SensorHub::refresh()
{
    std::uint16_t raw = 0;
    bool ok = true;
    for (std::uint8_t ch = 0; ch < kSensorChannels; ++ch) {
        if (bus_.read(ch, raw)) {
            values_[ch] = raw;
            stale_[ch] = 0;
        } else {
            ok = false;
            if (stale_[ch] < 255) {
                ++stale_[ch];
            }
        }
    }
    return ok;
}

std::uint16_t SensorHub::value(std::uint8_t channel) const
{
    return channel < kSensorChannels ? values_[channel] : 0;
}

bool SensorHub::stale(std::uint8_t channel) const
{
    return channel >= kSensorChannels || stale_[channel] >= kSensorStaleLimit;
}

// ------------------------------------------------------------------------------------------ cam phasers
namespace {
template <int Bank, bool Intake>
class CamPhaser {
public:
    static constexpr std::int16_t kMinDeg = Intake ? 0 : -40;
    static constexpr std::int16_t kMaxDeg = Intake ? 50 : 0;
    static constexpr std::int16_t kLockOilBar = 15;

    std::int16_t step(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10,
                      std::int16_t oil_c)
    {
        if (oil_bar_x10 < kLockOilBar || oil_c < 20 || rpm < 900) {
            locked_ = true;
            pid_.reset();
            target_ = Intake ? kMinDeg : kMaxDeg;
            return 0;
        }
        locked_ = false;
        target_ = target_for(rpm, load_pct);
        const std::int16_t error = static_cast<std::int16_t>(target_ - actual_deg);
        if (detail::abs_value(error) > 15) {
            ++deviation_count_;
        } else if (deviation_count_ > 0) {
            --deviation_count_;
        }
        return pid_.step(target_, actual_deg, false);
    }
    bool locked() const { return locked_; }
    std::int16_t target() const { return target_; }
    bool deviation_fault() const { return deviation_count_ >= 50; }
    void reset()
    {
        pid_.reset();
        locked_ = true;
        target_ = 0;
        deviation_count_ = 0;
    }

private:
    static std::int16_t target_for(std::int16_t rpm, std::int16_t load_pct)
    {
        std::int32_t t;
        if (Intake) {
            t = load_pct > 70 ? (rpm < 3500 ? 45 : 20) : (load_pct > 30 ? 30 : 10);
        } else {
            t = load_pct > 70 ? -10 : (rpm < 2000 ? -30 : -20);
        }
        if (Bank == 2) {
            t += 1;      // bank 2 timing offset of the cam drive
        }
        return detail::clamp<std::int16_t>(static_cast<std::int16_t>(t), kMinDeg, kMaxDeg);
    }
    detail::Pid<std::int16_t> pid_{180, 6, 20, -100, 100};
    bool locked_ = true;
    std::int16_t target_ = 0;
    std::uint16_t deviation_count_ = 0;
};

CamPhaser<1, true> g_cam_b1_intake;

CamPhaser<1, false> g_cam_b1_exhaust;

CamPhaser<2, true> g_cam_b2_intake;

CamPhaser<2, false> g_cam_b2_exhaust;
}  // namespace

std::int16_t cam_phaser_b1_intake(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10,
                                std::int16_t oil_c)
{
    return g_cam_b1_intake.step(rpm, load_pct, actual_deg, oil_bar_x10, oil_c);
}

bool cam_phaser_b1_intake_locked()
{
    return g_cam_b1_intake.locked();
}

std::int16_t cam_phaser_b1_intake_target()
{
    return g_cam_b1_intake.target();
}

bool cam_phaser_b1_intake_fault()
{
    return g_cam_b1_intake.deviation_fault();
}

void cam_phaser_b1_intake_reset()
{
    g_cam_b1_intake.reset();
}

std::int16_t cam_phaser_b1_exhaust(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10,
                                std::int16_t oil_c)
{
    return g_cam_b1_exhaust.step(rpm, load_pct, actual_deg, oil_bar_x10, oil_c);
}

bool cam_phaser_b1_exhaust_locked()
{
    return g_cam_b1_exhaust.locked();
}

std::int16_t cam_phaser_b1_exhaust_target()
{
    return g_cam_b1_exhaust.target();
}

bool cam_phaser_b1_exhaust_fault()
{
    return g_cam_b1_exhaust.deviation_fault();
}

void cam_phaser_b1_exhaust_reset()
{
    g_cam_b1_exhaust.reset();
}

std::int16_t cam_phaser_b2_intake(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10,
                                std::int16_t oil_c)
{
    return g_cam_b2_intake.step(rpm, load_pct, actual_deg, oil_bar_x10, oil_c);
}

bool cam_phaser_b2_intake_locked()
{
    return g_cam_b2_intake.locked();
}

std::int16_t cam_phaser_b2_intake_target()
{
    return g_cam_b2_intake.target();
}

bool cam_phaser_b2_intake_fault()
{
    return g_cam_b2_intake.deviation_fault();
}

void cam_phaser_b2_intake_reset()
{
    g_cam_b2_intake.reset();
}

std::int16_t cam_phaser_b2_exhaust(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10,
                                std::int16_t oil_c)
{
    return g_cam_b2_exhaust.step(rpm, load_pct, actual_deg, oil_bar_x10, oil_c);
}

bool cam_phaser_b2_exhaust_locked()
{
    return g_cam_b2_exhaust.locked();
}

std::int16_t cam_phaser_b2_exhaust_target()
{
    return g_cam_b2_exhaust.target();
}

bool cam_phaser_b2_exhaust_fault()
{
    return g_cam_b2_exhaust.deviation_fault();
}

void cam_phaser_b2_exhaust_reset()
{
    g_cam_b2_exhaust.reset();
}

// ------------------------------------------------------------------------------------------ injector 1 diagnostics
namespace {
std::uint8_t g_inj1_open_count;
std::uint8_t g_inj1_short_count;
std::int16_t g_inj1_balance;
}  // namespace

InjectorFault injector_1_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta)
{
    if (commanded && current_ma < kInjectorOpenMa) {
        g_inj1_open_count = static_cast<std::uint8_t>(std::min(g_inj1_open_count + 1, 255));
    } else if (commanded && current_ma > kInjectorShortMa) {
        g_inj1_short_count = static_cast<std::uint8_t>(std::min(g_inj1_short_count + 1, 255));
    } else if (!commanded && current_ma > kInjectorLeakMa) {
        g_inj1_short_count = static_cast<std::uint8_t>(std::min(g_inj1_short_count + 1, 255));
    } else {
        if (g_inj1_open_count > 0) {
            --g_inj1_open_count;
        }
        if (g_inj1_short_count > 0) {
            --g_inj1_short_count;
        }
    }
    g_inj1_balance = static_cast<std::int16_t>(detail::clamp<std::int32_t>(g_inj1_balance + crank_accel_delta / 8, -500, 500));
    if (g_inj1_open_count >= kInjectorFaultCount) {
        return InjectorFault::Open;
    }
    if (g_inj1_short_count >= kInjectorFaultCount) {
        return InjectorFault::Short;
    }
    if (detail::abs_value(g_inj1_balance) > kInjectorBalanceLimit) {
        return InjectorFault::Imbalance;
    }
    return InjectorFault::None;
}

std::int16_t injector_1_balance()
{
    return g_inj1_balance;
}

void injector_1_reset()
{
    g_inj1_open_count = 0;
    g_inj1_short_count = 0;
    g_inj1_balance = 0;
}

// ------------------------------------------------------------------------------------------ injector 2 diagnostics
namespace {
std::uint8_t g_inj2_open_count;
std::uint8_t g_inj2_short_count;
std::int16_t g_inj2_balance;
}  // namespace

InjectorFault injector_2_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta)
{
    if (commanded && current_ma < kInjectorOpenMa) {
        g_inj2_open_count = static_cast<std::uint8_t>(std::min(g_inj2_open_count + 1, 255));
    } else if (commanded && current_ma > kInjectorShortMa) {
        g_inj2_short_count = static_cast<std::uint8_t>(std::min(g_inj2_short_count + 1, 255));
    } else if (!commanded && current_ma > kInjectorLeakMa) {
        g_inj2_short_count = static_cast<std::uint8_t>(std::min(g_inj2_short_count + 1, 255));
    } else {
        if (g_inj2_open_count > 0) {
            --g_inj2_open_count;
        }
        if (g_inj2_short_count > 0) {
            --g_inj2_short_count;
        }
    }
    g_inj2_balance = static_cast<std::int16_t>(detail::clamp<std::int32_t>(g_inj2_balance + crank_accel_delta / 8, -500, 500));
    if (g_inj2_open_count >= kInjectorFaultCount) {
        return InjectorFault::Open;
    }
    if (g_inj2_short_count >= kInjectorFaultCount) {
        return InjectorFault::Short;
    }
    if (detail::abs_value(g_inj2_balance) > kInjectorBalanceLimit) {
        return InjectorFault::Imbalance;
    }
    return InjectorFault::None;
}

std::int16_t injector_2_balance()
{
    return g_inj2_balance;
}

void injector_2_reset()
{
    g_inj2_open_count = 0;
    g_inj2_short_count = 0;
    g_inj2_balance = 0;
}

// ------------------------------------------------------------------------------------------ injector 3 diagnostics
namespace {
std::uint8_t g_inj3_open_count;
std::uint8_t g_inj3_short_count;
std::int16_t g_inj3_balance;
}  // namespace

InjectorFault injector_3_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta)
{
    if (commanded && current_ma < kInjectorOpenMa) {
        g_inj3_open_count = static_cast<std::uint8_t>(std::min(g_inj3_open_count + 1, 255));
    } else if (commanded && current_ma > kInjectorShortMa) {
        g_inj3_short_count = static_cast<std::uint8_t>(std::min(g_inj3_short_count + 1, 255));
    } else if (!commanded && current_ma > kInjectorLeakMa) {
        g_inj3_short_count = static_cast<std::uint8_t>(std::min(g_inj3_short_count + 1, 255));
    } else {
        if (g_inj3_open_count > 0) {
            --g_inj3_open_count;
        }
        if (g_inj3_short_count > 0) {
            --g_inj3_short_count;
        }
    }
    g_inj3_balance = static_cast<std::int16_t>(detail::clamp<std::int32_t>(g_inj3_balance + crank_accel_delta / 8, -500, 500));
    if (g_inj3_open_count >= kInjectorFaultCount) {
        return InjectorFault::Open;
    }
    if (g_inj3_short_count >= kInjectorFaultCount) {
        return InjectorFault::Short;
    }
    if (detail::abs_value(g_inj3_balance) > kInjectorBalanceLimit) {
        return InjectorFault::Imbalance;
    }
    return InjectorFault::None;
}

std::int16_t injector_3_balance()
{
    return g_inj3_balance;
}

void injector_3_reset()
{
    g_inj3_open_count = 0;
    g_inj3_short_count = 0;
    g_inj3_balance = 0;
}

// ------------------------------------------------------------------------------------------ injector 4 diagnostics
namespace {
std::uint8_t g_inj4_open_count;
std::uint8_t g_inj4_short_count;
std::int16_t g_inj4_balance;
}  // namespace

InjectorFault injector_4_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta)
{
    if (commanded && current_ma < kInjectorOpenMa) {
        g_inj4_open_count = static_cast<std::uint8_t>(std::min(g_inj4_open_count + 1, 255));
    } else if (commanded && current_ma > kInjectorShortMa) {
        g_inj4_short_count = static_cast<std::uint8_t>(std::min(g_inj4_short_count + 1, 255));
    } else if (!commanded && current_ma > kInjectorLeakMa) {
        g_inj4_short_count = static_cast<std::uint8_t>(std::min(g_inj4_short_count + 1, 255));
    } else {
        if (g_inj4_open_count > 0) {
            --g_inj4_open_count;
        }
        if (g_inj4_short_count > 0) {
            --g_inj4_short_count;
        }
    }
    g_inj4_balance = static_cast<std::int16_t>(detail::clamp<std::int32_t>(g_inj4_balance + crank_accel_delta / 8, -500, 500));
    if (g_inj4_open_count >= kInjectorFaultCount) {
        return InjectorFault::Open;
    }
    if (g_inj4_short_count >= kInjectorFaultCount) {
        return InjectorFault::Short;
    }
    if (detail::abs_value(g_inj4_balance) > kInjectorBalanceLimit) {
        return InjectorFault::Imbalance;
    }
    return InjectorFault::None;
}

std::int16_t injector_4_balance()
{
    return g_inj4_balance;
}

void injector_4_reset()
{
    g_inj4_open_count = 0;
    g_inj4_short_count = 0;
    g_inj4_balance = 0;
}

// ------------------------------------------------------------------------------------------ wheel slip fl
namespace {
detail::Ema<std::int16_t, 1> g_slip_fl_filter;
std::uint16_t g_slip_fl_ms;
}  // namespace

std::int16_t wheel_slip_fl(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms)
{
    if (vehicle_kph_x10 < 30) {
        g_slip_fl_ms = 0;
        return 0;
    }
    std::int32_t slip = (static_cast<std::int32_t>(wheel_kph_x10) - vehicle_kph_x10) * 1000 / vehicle_kph_x10;
    if (!driven && slip > 0) {
        slip = 0;
    }
    const std::int16_t filtered = g_slip_fl_filter.update(static_cast<std::int16_t>(detail::clamp<std::int32_t>(slip, -1000, 1000)));
    if (detail::abs_value(filtered) > kSlipLimit) {
        g_slip_fl_ms = static_cast<std::uint16_t>(std::min<std::uint32_t>(g_slip_fl_ms + dt_ms, 65535u));
    } else {
        g_slip_fl_ms = 0;
    }
    return filtered;
}

bool wheel_slip_fl_active()
{
    return g_slip_fl_ms >= kSlipConfirmMs;
}

void wheel_slip_fl_reset()
{
    g_slip_fl_filter.reset();
    g_slip_fl_ms = 0;
}

// ------------------------------------------------------------------------------------------ wheel slip fr
namespace {
detail::Ema<std::int16_t, 1> g_slip_fr_filter;
std::uint16_t g_slip_fr_ms;
}  // namespace

std::int16_t wheel_slip_fr(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms)
{
    if (vehicle_kph_x10 < 30) {
        g_slip_fr_ms = 0;
        return 0;
    }
    std::int32_t slip = (static_cast<std::int32_t>(wheel_kph_x10) - vehicle_kph_x10) * 1000 / vehicle_kph_x10;
    if (!driven && slip > 0) {
        slip = 0;
    }
    const std::int16_t filtered = g_slip_fr_filter.update(static_cast<std::int16_t>(detail::clamp<std::int32_t>(slip, -1000, 1000)));
    if (detail::abs_value(filtered) > kSlipLimit) {
        g_slip_fr_ms = static_cast<std::uint16_t>(std::min<std::uint32_t>(g_slip_fr_ms + dt_ms, 65535u));
    } else {
        g_slip_fr_ms = 0;
    }
    return filtered;
}

bool wheel_slip_fr_active()
{
    return g_slip_fr_ms >= kSlipConfirmMs;
}

void wheel_slip_fr_reset()
{
    g_slip_fr_filter.reset();
    g_slip_fr_ms = 0;
}

// ------------------------------------------------------------------------------------------ wheel slip rl
namespace {
detail::Ema<std::int16_t, 1> g_slip_rl_filter;
std::uint16_t g_slip_rl_ms;
}  // namespace

std::int16_t wheel_slip_rl(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms)
{
    if (vehicle_kph_x10 < 30) {
        g_slip_rl_ms = 0;
        return 0;
    }
    std::int32_t slip = (static_cast<std::int32_t>(wheel_kph_x10) - vehicle_kph_x10) * 1000 / vehicle_kph_x10;
    if (!driven && slip > 0) {
        slip = 0;
    }
    const std::int16_t filtered = g_slip_rl_filter.update(static_cast<std::int16_t>(detail::clamp<std::int32_t>(slip, -1000, 1000)));
    if (detail::abs_value(filtered) > kSlipLimit) {
        g_slip_rl_ms = static_cast<std::uint16_t>(std::min<std::uint32_t>(g_slip_rl_ms + dt_ms, 65535u));
    } else {
        g_slip_rl_ms = 0;
    }
    return filtered;
}

bool wheel_slip_rl_active()
{
    return g_slip_rl_ms >= kSlipConfirmMs;
}

void wheel_slip_rl_reset()
{
    g_slip_rl_filter.reset();
    g_slip_rl_ms = 0;
}

// ------------------------------------------------------------------------------------------ wheel slip rr
namespace {
detail::Ema<std::int16_t, 1> g_slip_rr_filter;
std::uint16_t g_slip_rr_ms;
}  // namespace

std::int16_t wheel_slip_rr(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms)
{
    if (vehicle_kph_x10 < 30) {
        g_slip_rr_ms = 0;
        return 0;
    }
    std::int32_t slip = (static_cast<std::int32_t>(wheel_kph_x10) - vehicle_kph_x10) * 1000 / vehicle_kph_x10;
    if (!driven && slip > 0) {
        slip = 0;
    }
    const std::int16_t filtered = g_slip_rr_filter.update(static_cast<std::int16_t>(detail::clamp<std::int32_t>(slip, -1000, 1000)));
    if (detail::abs_value(filtered) > kSlipLimit) {
        g_slip_rr_ms = static_cast<std::uint16_t>(std::min<std::uint32_t>(g_slip_rr_ms + dt_ms, 65535u));
    } else {
        g_slip_rr_ms = 0;
    }
    return filtered;
}

bool wheel_slip_rr_active()
{
    return g_slip_rr_ms >= kSlipConfirmMs;
}

void wheel_slip_rr_reset()
{
    g_slip_rr_filter.reset();
    g_slip_rr_ms = 0;
}

std::int16_t traction_torque_limit(std::int16_t requested, std::int16_t slip_max, bool tcs_off)
{
    if (tcs_off || slip_max <= kSlipLimit) {
        return requested;
    }
    const std::int32_t excess = slip_max - kSlipLimit;
    const std::int32_t cut = excess * 2;
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(requested - cut, 0, requested));
}

// ------------------------------------------------------------------------------------------ EGR
EgrValve::EgrValve() : pid_(120, 5, 10, 0, 100) {}

std::uint8_t EgrValve::step(std::int16_t rpm, std::int16_t load_pct, std::int16_t coolant_c, std::int16_t measured_pct)
{
    if (coolant_c < kEgrMinCoolant || load_pct > kEgrMaxLoad || rpm > kEgrMaxRpm) {
        pid_.reset();
        target_ = 0;
        return 0;
    }
    target_ = static_cast<std::int16_t>(detail::clamp<std::int32_t>(40 - load_pct / 2 + (rpm < 1500 ? 10 : 0), 0, 40));
    return static_cast<std::uint8_t>(pid_.step(target_, measured_pct, false));
}

std::int16_t EgrValve::target() const
{
    return target_;
}

void EgrValve::reset()
{
    pid_.reset();
    target_ = 0;
}

// ------------------------------------------------------------------------------------------ particulate filter
DpfState ParticulateFilter::step(std::int16_t soot_mg_per_s, std::int16_t exhaust_c, std::int16_t speed_kph,
                                 std::int16_t diff_pressure_hpa, std::uint16_t dt_ms)
{
    const std::int32_t burn = exhaust_c > kDpfBurnC ? (exhaust_c - kDpfBurnC) / 10 : 0;
    soot_mg_ = detail::clamp<std::int32_t>(soot_mg_ + (soot_mg_per_s - burn) * dt_ms / 1000, 0, kDpfMaxSootMg);
    if (diff_pressure_hpa > kDpfBlockedHpa) {
        state_ = DpfState::Blocked;
        return state_;
    }
    switch (state_) {
    case DpfState::Loading:
        if (soot_mg_ > kDpfRegenSootMg && speed_kph > kDpfRegenMinKph) {
            state_ = DpfState::Regenerating;
            regen_ms_ = 0;
        } else if (soot_mg_ > kDpfRegenSootMg) {
            state_ = DpfState::RegenRequested;
        }
        break;
    case DpfState::RegenRequested:
        if (speed_kph > kDpfRegenMinKph) {
            state_ = DpfState::Regenerating;
            regen_ms_ = 0;
        } else if (soot_mg_ > kDpfMaxSootMg * 9 / 10) {
            state_ = DpfState::Blocked;
        }
        break;
    case DpfState::Regenerating:
        regen_ms_ = std::min<std::uint32_t>(regen_ms_ + dt_ms, 3600000u);
        if (soot_mg_ < kDpfCleanSootMg) {
            state_ = DpfState::Loading;
            ++regenerations_;
        } else if (speed_kph < kDpfRegenMinKph / 2 || regen_ms_ > kDpfRegenMaxMs) {
            state_ = DpfState::RegenRequested;
        }
        break;
    case DpfState::Blocked:
        if (diff_pressure_hpa < kDpfBlockedHpa / 2 && soot_mg_ < kDpfCleanSootMg) {
            state_ = DpfState::Loading;
        }
        break;
    default:
        state_ = DpfState::Loading;
        break;
    }
    return state_;
}

std::int32_t ParticulateFilter::soot() const
{
    return soot_mg_;
}

std::uint16_t ParticulateFilter::regenerations() const
{
    return regenerations_;
}

void ParticulateFilter::set_soot(std::int32_t mg)
{
    soot_mg_ = detail::clamp<std::int32_t>(mg, 0, kDpfMaxSootMg);
}

void ParticulateFilter::reset()
{
    state_ = DpfState::Loading;
    soot_mg_ = 0;
    regen_ms_ = 0;
    regenerations_ = 0;
}

// ------------------------------------------------------------------------------------------ SCR dosing
std::uint16_t scr_dosing(std::int16_t nox_ppm_in, std::int16_t exhaust_flow_kgh, std::int16_t catalyst_c, std::int16_t tank_pct,
                         bool frozen)
{
    if (frozen || catalyst_c < kScrMinC || tank_pct <= 0) {
        return 0;
    }
    std::int32_t dose = static_cast<std::int32_t>(nox_ppm_in) * exhaust_flow_kgh / 1000;
    if (catalyst_c > kScrMaxC) {
        dose = dose * 80 / 100;
    }
    if (tank_pct < 10) {
        dose = dose * 90 / 100;
    }
    return static_cast<std::uint16_t>(detail::clamp<std::int32_t>(dose, 0, kScrMaxDose));
}

// ------------------------------------------------------------------------------------------ evaporative purge
std::uint8_t evap_purge(std::int16_t coolant_c, bool closed_loop, std::int16_t load_pct, std::int16_t trim_pct,
                        std::uint32_t since_start_ms)
{
    if (!closed_loop || coolant_c < 60 || since_start_ms < 60000u) {
        return 0;
    }
    std::int32_t duty = 10 + load_pct / 2;
    if (trim_pct < -15) {
        duty /= 2;
    }
    if (trim_pct < -25) {
        duty = 0;
    }
    return static_cast<std::uint8_t>(detail::clamp<std::int32_t>(duty, 0, 60));
}

// ------------------------------------------------------------------------------------------ fuel trim adaptation
namespace {
template <typename T, std::size_t NR, std::size_t NL>
class AdaptiveTable {
public:
    constexpr AdaptiveTable(const std::array<std::int16_t, NR>& rpm, const std::array<std::int16_t, NL>& load)
        : rpm_(rpm), load_(load) {}
    bool learn(std::int16_t rpm, std::int16_t load, T short_term, T limit)
    {
        const std::size_t r = cell(rpm_, rpm);
        const std::size_t l = cell(load_, load);
        T& v = cells_[r][l];
        const std::int32_t next = v + short_term / 8;
        v = static_cast<T>(detail::clamp<std::int32_t>(next, -limit, limit));
        ++updates_;
        return next > limit || next < -limit;
    }
    T value(std::int16_t rpm, std::int16_t load) const { return cells_[cell(rpm_, rpm)][cell(load_, load)]; }
    std::uint32_t updates() const { return updates_; }
    void clear()
    {
        for (auto& row : cells_) {
            row.fill(T{});
        }
        updates_ = 0;
    }

private:
    template <std::size_t N>
    static std::size_t cell(const std::array<std::int16_t, N>& axis, std::int16_t v)
    {
        std::size_t i = 0;
        while (i + 1 < N && v >= axis[i + 1]) {
            ++i;
        }
        return i;
    }
    std::array<std::int16_t, NR> rpm_;
    std::array<std::int16_t, NL> load_;
    std::array<std::array<T, NL>, NR> cells_{};
    std::uint32_t updates_ = 0;
};

AdaptiveTable<std::int16_t, 4, 3> g_fuel_trim({0, 1500, 3000, 4500}, {0, 30, 60});
AdaptiveTable<std::int16_t, 3, 3> g_knock_adapt({0, 2500, 4500}, {0, 40, 70});
}  // namespace

bool fuel_trim_learn(std::int16_t rpm, std::int16_t load_pct, std::int16_t short_term_pct_x10, bool closed_loop, std::int16_t coolant_c)
{
    if (!closed_loop || coolant_c < 70) {
        return false;
    }
    return g_fuel_trim.learn(rpm, load_pct, short_term_pct_x10, kTrimLimit);
}

std::int16_t fuel_trim(std::int16_t rpm, std::int16_t load_pct)
{
    return g_fuel_trim.value(rpm, load_pct);
}

bool knock_adapt_learn(std::int16_t rpm, std::int16_t load_pct, std::int16_t retard_x10)
{
    return g_knock_adapt.learn(rpm, load_pct, retard_x10, kKnockAdaptLimit);
}

std::int16_t knock_adapt(std::int16_t rpm, std::int16_t load_pct)
{
    return g_knock_adapt.value(rpm, load_pct);
}

std::uint32_t adaptation_updates()
{
    return g_fuel_trim.updates() + g_knock_adapt.updates();
}

void adaptation_reset()
{
    g_fuel_trim.clear();
    g_knock_adapt.clear();
}

// ------------------------------------------------------------------------------------------ hybrid battery
BatteryState::BatteryState(std::int32_t capacity_mas) : capacity_mas_(capacity_mas > 0 ? capacity_mas : 1) {}

std::int16_t BatteryState::update(std::int32_t current_ma, std::int16_t voltage_mv_cell, bool resting, std::uint16_t dt_ms)
{
    charge_mas_ -= static_cast<std::int64_t>(current_ma) * dt_ms / 1000;
    charge_mas_ = std::max<std::int64_t>(0, std::min<std::int64_t>(charge_mas_, capacity_mas_));
    if (resting) {
        rest_ms_ = std::min<std::uint32_t>(rest_ms_ + dt_ms, 3600000u);
        if (rest_ms_ >= kOcvRestMs) {
            const std::int16_t ocv_soc = ocv_to_soc(voltage_mv_cell);
            const std::int64_t ocv_charge = static_cast<std::int64_t>(capacity_mas_) * ocv_soc / 1000;
            charge_mas_ = (charge_mas_ * 3 + ocv_charge) / 4;
        }
    } else {
        rest_ms_ = 0;
    }
    return soc();
}

std::int16_t BatteryState::soc() const
{
    return static_cast<std::int16_t>(charge_mas_ * 1000 / capacity_mas_);
}

void BatteryState::set_soc(std::int16_t permille)
{
    charge_mas_ = static_cast<std::int64_t>(capacity_mas_) * detail::clamp<std::int16_t>(permille, 0, 1000) / 1000;
}

std::int16_t BatteryState::ocv_to_soc(std::int16_t mv)
{
    static constexpr detail::Lut<std::int16_t, 6> kOcv({3300, 3500, 3600, 3700, 3900, 4150}, {0, 100, 300, 500, 800, 1000});
    return kOcv(mv);
}

void BatteryState::reset()
{
    charge_mas_ = 0;
    rest_ms_ = 0;
}

// ------------------------------------------------------------------------------------------ regenerative braking
RegenSplit regen_split(std::int16_t brake_request_nm, std::int16_t soc_permille, std::int16_t speed_kph, std::int16_t motor_max_nm,
                       bool abs_active, std::int16_t battery_c)
{
    RegenSplit s{};
    std::int32_t regen_max = motor_max_nm;
    if (abs_active || speed_kph < kRegenMinKph) {
        regen_max = 0;
    } else if (soc_permille > kRegenFullSoc) {
        regen_max = 0;
    } else if (soc_permille > kRegenTaperSoc) {
        regen_max = regen_max * (kRegenFullSoc - soc_permille) / (kRegenFullSoc - kRegenTaperSoc);
    }
    if (battery_c < 0) {
        regen_max /= 2;
    } else if (battery_c > 50) {
        regen_max = regen_max * 3 / 4;
    }
    s.regen_nm = static_cast<std::int16_t>(std::min<std::int32_t>(brake_request_nm, regen_max));
    s.friction_nm = static_cast<std::int16_t>(brake_request_nm - s.regen_nm);
    return s;
}

// ------------------------------------------------------------------------------------------ alternator
std::int16_t alternator_target_mv(std::int16_t battery_c, bool engine_running, bool decelerating, bool high_load,
                                  std::int16_t soc_pct)
{
    if (!engine_running) {
        return 0;
    }
    std::int32_t mv = 14200 - (battery_c - 25) * 10;
    if (decelerating) {
        mv = 14800;
    } else if (!high_load && soc_pct > 85) {
        mv = 12800;
    }
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(mv, 12500, 14900));
}

// ------------------------------------------------------------------------------------------ starter
StarterState Starter::step(bool request, bool clutch_or_park, bool immo_released, std::int16_t rpm, std::uint16_t dt_ms)
{
    switch (state_) {
    case StarterState::Idle:
        if (request && clutch_or_park && immo_released && rpm == 0 && cool_ms_ == 0) {
            state_ = StarterState::Cranking;
            crank_ms_ = 0;
        } else if (cool_ms_ > 0) {
            cool_ms_ = cool_ms_ > dt_ms ? static_cast<std::uint16_t>(cool_ms_ - dt_ms) : 0;
        }
        break;
    case StarterState::Cranking:
        crank_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(crank_ms_ + dt_ms, 65535u));
        if (rpm > kStarterReleaseRpm) {
            state_ = StarterState::Idle;
        } else if (crank_ms_ > kStarterMaxMs || !clutch_or_park) {
            state_ = StarterState::Protect;
            cool_ms_ = kStarterCoolMs;
        }
        break;
    case StarterState::Protect:
        state_ = StarterState::Idle;
        break;
    default:
        state_ = StarterState::Idle;
        break;
    }
    return state_;
}

void Starter::reset()
{
    state_ = StarterState::Idle;
    crank_ms_ = 0;
    cool_ms_ = 0;
}

// ------------------------------------------------------------------------------------------ launch control
std::int16_t launch_rpm_limit(bool armed, std::int16_t speed_kph, std::int16_t pedal_pct, std::int16_t clutch_pct, bool tcs_on)
{
    if (!armed || speed_kph > 5 || !tcs_on) {
        return kRevLimitRpm;
    }
    if (pedal_pct > 90 && clutch_pct < 10) {
        return 4000;
    }
    return kRevLimitRpm;
}

// ------------------------------------------------------------------------------------------ hill hold
bool HillHold::step(std::int16_t slope_pct, bool brake, std::int16_t speed_kph, std::int16_t engine_torque, std::uint16_t dt_ms)
{
    if (speed_kph > 0) {
        holding_ = false;
        hold_ms_ = 0;
        return false;
    }
    if (brake && detail::abs_value(slope_pct) >= kHillMinSlope) {
        holding_ = true;
        hold_ms_ = 0;
        return true;
    }
    if (holding_) {
        hold_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(hold_ms_ + dt_ms, 65535u));
        if (hold_ms_ > kHillHoldMs || engine_torque > slope_pct * kHillTorquePerPct) {
            holding_ = false;
        }
    }
    return holding_;
}

void HillHold::reset()
{
    holding_ = false;
    hold_ms_ = 0;
}

// ------------------------------------------------------------------------------------------ speed limiter
std::int16_t speed_limiter_torque(bool active, std::int16_t limit_kph, std::int16_t speed_kph, std::int16_t pedal_pct,
                                  std::int16_t driver_nm)
{
    if (!active || pedal_pct > 95) {
        return driver_nm;
    }
    const std::int32_t margin = limit_kph - speed_kph;
    if (margin <= 0) {
        return 0;
    }
    if (margin < 5) {
        return static_cast<std::int16_t>(std::min<std::int32_t>(driver_nm, margin * 20));
    }
    return driver_nm;
}

// ------------------------------------------------------------------------------------------ service interval
ServiceDue service_due(std::uint32_t km_since, std::uint32_t days_since, std::uint32_t oil_quality_pct)
{
    if (km_since >= kServiceKm || days_since >= kServiceDays || oil_quality_pct < 10) {
        return ServiceDue::Now;
    }
    if (km_since + 1500 >= kServiceKm || days_since + 30 >= kServiceDays || oil_quality_pct < 25) {
        return ServiceDue::Soon;
    }
    return ServiceDue::No;
}

// ------------------------------------------------------------------------------------------ oil level
std::int16_t oil_level_mm(std::int16_t raw_mm, std::int16_t oil_c, bool engine_off_minutes, std::int16_t slope_pct)
{
    if (!engine_off_minutes || detail::abs_value(slope_pct) > 5) {
        return -1;
    }
    std::int32_t level = raw_mm - (oil_c - 20) / 10;
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(level, 0, 120));
}

// ------------------------------------------------------------------------------------------ drive modes
DriveProfile drive_profile(DriveMode mode, bool trailer, std::int16_t outside_c)
{
    DriveProfile p{};
    switch (mode) {
    case DriveMode::Sport:
        p.pedal_gain_pct = 130;
        p.shift_mode = DriveMode::Sport;
        p.stop_start = false;
        break;
    case DriveMode::Manual:
        p.pedal_gain_pct = 115;
        p.shift_mode = DriveMode::Manual;
        p.stop_start = false;
        break;
    case DriveMode::Eco:
    default:
        p.pedal_gain_pct = 85;
        p.shift_mode = DriveMode::Eco;
        p.stop_start = true;
        break;
    }
    if (trailer) {
        p.pedal_gain_pct = static_cast<std::int16_t>(std::min<std::int32_t>(p.pedal_gain_pct, 100));
        p.stop_start = false;
    }
    if (outside_c < -10) {
        p.stop_start = false;
    }
    return p;
}

// ------------------------------------------------------------------------------------------ dual clutch slip control
ClutchControl::ClutchControl() : pid_(140, 4, 12, 0, 1000) {}

std::int16_t ClutchControl::launch(std::int16_t engine_rpm, std::int16_t input_rpm, std::int16_t pedal_pct, std::int16_t clutch_c)
{
    if (clutch_c > kClutchHotC) {
        overheated_ = true;
    } else if (clutch_c < kClutchHotC - 30) {
        overheated_ = false;
    }
    const std::int16_t target_slip = overheated_ ? 50 : static_cast<std::int16_t>(100 + pedal_pct * 4);
    const std::int16_t slip = static_cast<std::int16_t>(engine_rpm - input_rpm);
    if (slip < 30 && input_rpm > 1000) {
        closed_ = true;
        return 1000;
    }
    closed_ = false;
    return static_cast<std::int16_t>(1000 - pid_.step(target_slip, slip, false));
}

bool ClutchControl::closed() const
{
    return closed_;
}

bool ClutchControl::overheated() const
{
    return overheated_;
}

void ClutchControl::reset()
{
    pid_.reset();
    closed_ = false;
    overheated_ = false;
}

// ------------------------------------------------------------------------------------------ gear actuator
GearActuator::Result GearActuator::engage(std::int16_t target_gear, std::int16_t position_mm, std::int16_t speed_rpm_out,
                                          std::uint16_t dt_ms)
{
    Result r{};
    if (target_gear < -1 || target_gear > 7) {
        r.fault = true;
        return r;
    }
    const std::int16_t want = target_gear == 0 ? 0 : (target_gear > 0 ? (target_gear % 2 == 1 ? 8 : -8) : -8);
    if (target_gear != current_target_) {
        current_target_ = target_gear;
        time_ms_ = 0;
        synchronizing_ = speed_rpm_out > 0;
    }
    time_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(time_ms_ + dt_ms, 65535u));
    const std::int16_t error = static_cast<std::int16_t>(want - position_mm);
    if (detail::abs_value(error) <= 1) {
        r.engaged = true;
        synchronizing_ = false;
        r.force = 0;
        return r;
    }
    if (time_ms_ > kGearTimeoutMs) {
        r.fault = true;
        return r;
    }
    r.force = static_cast<std::int16_t>(detail::clamp<std::int32_t>(error * (synchronizing_ ? 20 : 60), -500, 500));
    return r;
}

void GearActuator::reset()
{
    current_target_ = 0;
    time_ms_ = 0;
    synchronizing_ = false;
}

// ------------------------------------------------------------------------------------------ calibration parameter sets
namespace {
template <typename T, std::size_t N>
class ParamSet {
public:
    constexpr ParamSet(const std::array<T, N>& defaults, const std::array<T, N>& lo, const std::array<T, N>& hi)
        : values_(defaults), defaults_(defaults), lo_(lo), hi_(hi) {}
    bool set(std::size_t index, T value)
    {
        if (index >= N || value < lo_[index] || value > hi_[index]) {
            return false;
        }
        values_[index] = value;
        return true;
    }
    T get(std::size_t index) const { return index < N ? values_[index] : T{}; }
    std::uint16_t checksum() const
    {
        std::uint16_t sum = 0xA5A5u;
        for (const T& v : values_) {
            sum = static_cast<std::uint16_t>((sum << 1) ^ static_cast<std::uint16_t>(v) ^ (sum >> 15));
        }
        return sum;
    }
    bool verify(std::uint16_t stored) const { return stored == checksum(); }
    void restore() { values_ = defaults_; }

private:
    std::array<T, N> values_;
    std::array<T, N> defaults_;
    std::array<T, N> lo_;
    std::array<T, N> hi_;
};

ParamSet<std::int16_t, 4> g_idle_params({800, 50, 100, 500}, {600, 0, 0, 300}, {1200, 200, 300, 900});
ParamSet<std::int16_t, 3> g_limit_params({6500, 250, 180}, {5000, 100, 100}, {7500, 400, 250});
ParamSet<std::uint16_t, 3> g_timing_params({500, 2000, 60000}, {100, 500, 10000}, {2000, 10000, 65000});
}  // namespace

bool idle_param_set(std::uint8_t index, std::int16_t value)
{
    return g_idle_params.set(index, value);
}

std::int16_t idle_param(std::uint8_t index)
{
    return g_idle_params.get(index);
}

bool limit_param_set(std::uint8_t index, std::int16_t value)
{
    return g_limit_params.set(index, value);
}

std::int16_t limit_param(std::uint8_t index)
{
    return g_limit_params.get(index);
}

bool timing_param_set(std::uint8_t index, std::uint16_t value)
{
    return g_timing_params.set(index, value);
}

std::uint16_t timing_param(std::uint8_t index)
{
    return g_timing_params.get(index);
}

ParamCheck params_check(std::uint16_t idle_crc, std::uint16_t limit_crc, std::uint16_t timing_crc)
{
    ParamCheck c{};
    c.idle_ok = g_idle_params.verify(idle_crc);
    c.limit_ok = g_limit_params.verify(limit_crc);
    c.timing_ok = g_timing_params.verify(timing_crc);
    if (!c.idle_ok) {
        g_idle_params.restore();
    }
    if (!c.limit_ok) {
        g_limit_params.restore();
    }
    if (!c.timing_ok) {
        g_timing_params.restore();
    }
    return c;
}

std::uint16_t params_crc(std::uint8_t set)
{
    switch (set) {
    case 0:
        return g_idle_params.checksum();
    case 1:
        return g_limit_params.checksum();
    case 2:
        return g_timing_params.checksum();
    default:
        return 0;
    }
}

void params_restore()
{
    g_idle_params.restore();
    g_limit_params.restore();
    g_timing_params.restore();
}

// ------------------------------------------------------------------------------------------ OBD readiness
std::uint8_t Readiness::complete(ReadinessMonitor m, bool passed)
{
    const auto bit = static_cast<std::uint8_t>(1u << static_cast<unsigned>(m));
    if (static_cast<unsigned>(m) >= 8u) {
        return complete_;
    }
    complete_ = static_cast<std::uint8_t>(complete_ | bit);
    if (passed) {
        failed_ = static_cast<std::uint8_t>(failed_ & ~bit);
    } else {
        failed_ = static_cast<std::uint8_t>(failed_ | bit);
    }
    return complete_;
}

bool Readiness::ready(std::uint8_t required_mask) const
{
    return (complete_ & required_mask) == required_mask && (failed_ & required_mask) == 0;
}

bool Readiness::mil() const
{
    return failed_ != 0;
}

void Readiness::new_cycle()
{
    complete_ = 0;
}

void Readiness::clear()
{
    complete_ = 0;
    failed_ = 0;
}

// ------------------------------------------------------------------------------------------ CAN message transmission
namespace {
using TransmissionSigGear = detail::Signal<0, 4, 1, 0, 0, 7>;
using TransmissionSigTargetGear = detail::Signal<4, 4, 1, 0, 0, 7>;
using TransmissionSigOilTemp = detail::Signal<8, 8, 1, -40, -40, 160>;
using TransmissionSigOutputRpm = detail::Signal<16, 14, 1, 0, 0, 10000>;
using TransmissionSigShifting = detail::Signal<30, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_transmission(const TransmissionMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return TransmissionSigGear::encode(frame, m.gear) &&
           TransmissionSigTargetGear::encode(frame, m.targetGear) &&
           TransmissionSigOilTemp::encode(frame, m.oilTemp) &&
           TransmissionSigOutputRpm::encode(frame, m.outputRpm) &&
           TransmissionSigShifting::encode(frame, m.shifting);
}

TransmissionMsg unpack_transmission(const std::array<std::uint8_t, 8>& frame)
{
    TransmissionMsg m{};
    m.gear = TransmissionSigGear::decode(frame);
    m.targetGear = TransmissionSigTargetGear::decode(frame);
    m.oilTemp = TransmissionSigOilTemp::decode(frame);
    m.outputRpm = TransmissionSigOutputRpm::decode(frame);
    m.shifting = TransmissionSigShifting::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message brake
namespace {
using BrakeSigPressure = detail::Signal<0, 12, 1, 0, 0, 250>;
using BrakeSigAbs = detail::Signal<12, 1, 1, 0, 0, 1>;
using BrakeSigEsp = detail::Signal<13, 1, 1, 0, 0, 1>;
using BrakeSigDecel = detail::Signal<16, 12, 1, 0, 0, 2000>;
using BrakeSigPedalSwitch = detail::Signal<28, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_brake(const BrakeMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return BrakeSigPressure::encode(frame, m.pressure) &&
           BrakeSigAbs::encode(frame, m.abs) &&
           BrakeSigEsp::encode(frame, m.esp) &&
           BrakeSigDecel::encode(frame, m.decel) &&
           BrakeSigPedalSwitch::encode(frame, m.pedalSwitch);
}

BrakeMsg unpack_brake(const std::array<std::uint8_t, 8>& frame)
{
    BrakeMsg m{};
    m.pressure = BrakeSigPressure::decode(frame);
    m.abs = BrakeSigAbs::decode(frame);
    m.esp = BrakeSigEsp::decode(frame);
    m.decel = BrakeSigDecel::decode(frame);
    m.pedalSwitch = BrakeSigPedalSwitch::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message wheel_speeds
namespace {
using WheelSpeedsSigFL = detail::Signal<0, 14, 1, 0, 0, 3000>;
using WheelSpeedsSigFR = detail::Signal<14, 14, 1, 0, 0, 3000>;
using WheelSpeedsSigRL = detail::Signal<28, 14, 1, 0, 0, 3000>;
using WheelSpeedsSigRR = detail::Signal<42, 14, 1, 0, 0, 3000>;
}  // namespace

bool pack_wheel_speeds(const WheelSpeedsMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return WheelSpeedsSigFL::encode(frame, m.fL) &&
           WheelSpeedsSigFR::encode(frame, m.fR) &&
           WheelSpeedsSigRL::encode(frame, m.rL) &&
           WheelSpeedsSigRR::encode(frame, m.rR);
}

WheelSpeedsMsg unpack_wheel_speeds(const std::array<std::uint8_t, 8>& frame)
{
    WheelSpeedsMsg m{};
    m.fL = WheelSpeedsSigFL::decode(frame);
    m.fR = WheelSpeedsSigFR::decode(frame);
    m.rL = WheelSpeedsSigRL::decode(frame);
    m.rR = WheelSpeedsSigRR::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message hybrid
namespace {
using HybridSigSoc = detail::Signal<0, 10, 1, 0, 0, 1000>;
using HybridSigMotorNm = detail::Signal<10, 12, 1, -1000, -1000, 1000>;
using HybridSigBatteryC = detail::Signal<22, 8, 1, -40, -40, 100>;
using HybridSigRegen = detail::Signal<30, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_hybrid(const HybridMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return HybridSigSoc::encode(frame, m.soc) &&
           HybridSigMotorNm::encode(frame, m.motorNm) &&
           HybridSigBatteryC::encode(frame, m.batteryC) &&
           HybridSigRegen::encode(frame, m.regen);
}

HybridMsg unpack_hybrid(const std::array<std::uint8_t, 8>& frame)
{
    HybridMsg m{};
    m.soc = HybridSigSoc::decode(frame);
    m.motorNm = HybridSigMotorNm::decode(frame);
    m.batteryC = HybridSigBatteryC::decode(frame);
    m.regen = HybridSigRegen::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message emissions
namespace {
using EmissionsSigSoot = detail::Signal<0, 16, 1, 0, 0, 40000>;
using EmissionsSigDpfState = detail::Signal<16, 2, 1, 0, 0, 3>;
using EmissionsSigScrDose = detail::Signal<18, 13, 1, 0, 0, 5000>;
using EmissionsSigEgr = detail::Signal<31, 7, 1, 0, 0, 100>;
}  // namespace

bool pack_emissions(const EmissionsMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return EmissionsSigSoot::encode(frame, m.soot) &&
           EmissionsSigDpfState::encode(frame, m.dpfState) &&
           EmissionsSigScrDose::encode(frame, m.scrDose) &&
           EmissionsSigEgr::encode(frame, m.egr);
}

EmissionsMsg unpack_emissions(const std::array<std::uint8_t, 8>& frame)
{
    EmissionsMsg m{};
    m.soot = EmissionsSigSoot::decode(frame);
    m.dpfState = EmissionsSigDpfState::decode(frame);
    m.scrDose = EmissionsSigScrDose::decode(frame);
    m.egr = EmissionsSigEgr::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message cruise
namespace {
using CruiseSigState = detail::Signal<0, 2, 1, 0, 0, 3>;
using CruiseSigSetKph = detail::Signal<2, 8, 1, 0, 0, 250>;
using CruiseSigLimit = detail::Signal<10, 8, 1, 0, 0, 250>;
using CruiseSigLimiterOn = detail::Signal<18, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_cruise(const CruiseMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return CruiseSigState::encode(frame, m.state) &&
           CruiseSigSetKph::encode(frame, m.setKph) &&
           CruiseSigLimit::encode(frame, m.limit) &&
           CruiseSigLimiterOn::encode(frame, m.limiterOn);
}

CruiseMsg unpack_cruise(const std::array<std::uint8_t, 8>& frame)
{
    CruiseMsg m{};
    m.state = CruiseSigState::decode(frame);
    m.setKph = CruiseSigSetKph::decode(frame);
    m.limit = CruiseSigLimit::decode(frame);
    m.limiterOn = CruiseSigLimiterOn::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ ignition coil 1
namespace {
std::uint16_t g_coil1_dwell_us = 2500;
std::uint8_t g_coil1_faults;
}  // namespace

std::uint16_t coil_1_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma)
{
    std::int32_t dwell = 2500;
    if (battery_mv < 11000) {
        dwell += (11000 - battery_mv) / 4;
    } else if (battery_mv > 14000) {
        dwell -= (battery_mv - 14000) / 8;
    }
    if (rpm > 5000) {
        dwell -= (rpm - 5000) / 4;
    }
    if (primary_peak_ma < kCoilMinPeakMa) {
        g_coil1_faults = static_cast<std::uint8_t>(std::min(g_coil1_faults + 1, 255));
    } else if (g_coil1_faults > 0) {
        --g_coil1_faults;
    }
    g_coil1_dwell_us = static_cast<std::uint16_t>(detail::clamp<std::int32_t>(dwell, 1200, 4000));
    return g_coil1_dwell_us;
}

bool coil_1_fault()
{
    return g_coil1_faults >= kCoilFaultCount;
}

void coil_1_reset()
{
    g_coil1_dwell_us = 2500;
    g_coil1_faults = 0;
}

// ------------------------------------------------------------------------------------------ ignition coil 2
namespace {
std::uint16_t g_coil2_dwell_us = 2500;
std::uint8_t g_coil2_faults;
}  // namespace

std::uint16_t coil_2_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma)
{
    std::int32_t dwell = 2500;
    if (battery_mv < 11000) {
        dwell += (11000 - battery_mv) / 4;
    } else if (battery_mv > 14000) {
        dwell -= (battery_mv - 14000) / 8;
    }
    if (rpm > 5000) {
        dwell -= (rpm - 5000) / 4;
    }
    if (primary_peak_ma < kCoilMinPeakMa) {
        g_coil2_faults = static_cast<std::uint8_t>(std::min(g_coil2_faults + 1, 255));
    } else if (g_coil2_faults > 0) {
        --g_coil2_faults;
    }
    g_coil2_dwell_us = static_cast<std::uint16_t>(detail::clamp<std::int32_t>(dwell, 1200, 4000));
    return g_coil2_dwell_us;
}

bool coil_2_fault()
{
    return g_coil2_faults >= kCoilFaultCount;
}

void coil_2_reset()
{
    g_coil2_dwell_us = 2500;
    g_coil2_faults = 0;
}

// ------------------------------------------------------------------------------------------ ignition coil 3
namespace {
std::uint16_t g_coil3_dwell_us = 2500;
std::uint8_t g_coil3_faults;
}  // namespace

std::uint16_t coil_3_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma)
{
    std::int32_t dwell = 2500;
    if (battery_mv < 11000) {
        dwell += (11000 - battery_mv) / 4;
    } else if (battery_mv > 14000) {
        dwell -= (battery_mv - 14000) / 8;
    }
    if (rpm > 5000) {
        dwell -= (rpm - 5000) / 4;
    }
    if (primary_peak_ma < kCoilMinPeakMa) {
        g_coil3_faults = static_cast<std::uint8_t>(std::min(g_coil3_faults + 1, 255));
    } else if (g_coil3_faults > 0) {
        --g_coil3_faults;
    }
    g_coil3_dwell_us = static_cast<std::uint16_t>(detail::clamp<std::int32_t>(dwell, 1200, 4000));
    return g_coil3_dwell_us;
}

bool coil_3_fault()
{
    return g_coil3_faults >= kCoilFaultCount;
}

void coil_3_reset()
{
    g_coil3_dwell_us = 2500;
    g_coil3_faults = 0;
}

// ------------------------------------------------------------------------------------------ ignition coil 4
namespace {
std::uint16_t g_coil4_dwell_us = 2500;
std::uint8_t g_coil4_faults;
}  // namespace

std::uint16_t coil_4_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma)
{
    std::int32_t dwell = 2500;
    if (battery_mv < 11000) {
        dwell += (11000 - battery_mv) / 4;
    } else if (battery_mv > 14000) {
        dwell -= (battery_mv - 14000) / 8;
    }
    if (rpm > 5000) {
        dwell -= (rpm - 5000) / 4;
    }
    if (primary_peak_ma < kCoilMinPeakMa) {
        g_coil4_faults = static_cast<std::uint8_t>(std::min(g_coil4_faults + 1, 255));
    } else if (g_coil4_faults > 0) {
        --g_coil4_faults;
    }
    g_coil4_dwell_us = static_cast<std::uint16_t>(detail::clamp<std::int32_t>(dwell, 1200, 4000));
    return g_coil4_dwell_us;
}

bool coil_4_fault()
{
    return g_coil4_faults >= kCoilFaultCount;
}

void coil_4_reset()
{
    g_coil4_dwell_us = 2500;
    g_coil4_faults = 0;
}

// ------------------------------------------------------------------------------------------ IntakeTempMonitor
namespace {
class IntakeTempMonitorImpl : public detail::Monitor<IntakeTempMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 1000;
    static constexpr std::uint16_t kHealMs = 2000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 30 || raw_ > 4050; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
IntakeTempMonitorImpl g_IntakeTempMonitor;
}  // namespace

MonitorState monitor_IntakeTempMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_IntakeTempMonitor.set(value, reference);
    return g_IntakeTempMonitor.update(dt_ms);
}

void monitor_IntakeTempMonitor_reset()
{
    g_IntakeTempMonitor.reset();
}

// ------------------------------------------------------------------------------------------ RailPressureMonitor
namespace {
class RailPressureMonitorImpl : public detail::Monitor<RailPressureMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 500;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < rpm_ - 100 || raw_ > rpm_ + 150; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
RailPressureMonitorImpl g_RailPressureMonitor;
}  // namespace

MonitorState monitor_RailPressureMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_RailPressureMonitor.set(value, reference);
    return g_RailPressureMonitor.update(dt_ms);
}

void monitor_RailPressureMonitor_reset()
{
    g_RailPressureMonitor.reset();
}

// ------------------------------------------------------------------------------------------ CamCrankMonitor
namespace {
class CamCrankMonitorImpl : public detail::Monitor<CamCrankMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 300;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return detail::abs_value<std::int16_t>(static_cast<std::int16_t>(raw_ - rpm_)) > 6; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
CamCrankMonitorImpl g_CamCrankMonitor;
}  // namespace

MonitorState monitor_CamCrankMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_CamCrankMonitor.set(value, reference);
    return g_CamCrankMonitor.update(dt_ms);
}

void monitor_CamCrankMonitor_reset()
{
    g_CamCrankMonitor.reset();
}

// ------------------------------------------------------------------------------------------ BatteryVoltageMonitor
namespace {
class BatteryVoltageMonitorImpl : public detail::Monitor<BatteryVoltageMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 2000;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 9000 || raw_ > 16000; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
BatteryVoltageMonitorImpl g_BatteryVoltageMonitor;
}  // namespace

MonitorState monitor_BatteryVoltageMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_BatteryVoltageMonitor.set(value, reference);
    return g_BatteryVoltageMonitor.update(dt_ms);
}

void monitor_BatteryVoltageMonitor_reset()
{
    g_BatteryVoltageMonitor.reset();
}

// ------------------------------------------------------------------------------------------ CatalystTempMonitor
namespace {
class CatalystTempMonitorImpl : public detail::Monitor<CatalystTempMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 3000;
    static constexpr std::uint16_t kHealMs = 5000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ > 950; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
CatalystTempMonitorImpl g_CatalystTempMonitor;
}  // namespace

MonitorState monitor_CatalystTempMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_CatalystTempMonitor.set(value, reference);
    return g_CatalystTempMonitor.update(dt_ms);
}

void monitor_CatalystTempMonitor_reset()
{
    g_CatalystTempMonitor.reset();
}

// ------------------------------------------------------------------------------------------ ClutchSwitchMonitor
namespace {
class ClutchSwitchMonitorImpl : public detail::Monitor<ClutchSwitchMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 5000;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ == 0 && rpm_ > 0; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
ClutchSwitchMonitorImpl g_ClutchSwitchMonitor;
}  // namespace

MonitorState monitor_ClutchSwitchMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_ClutchSwitchMonitor.set(value, reference);
    return g_ClutchSwitchMonitor.update(dt_ms);
}

void monitor_ClutchSwitchMonitor_reset()
{
    g_ClutchSwitchMonitor.reset();
}

// ------------------------------------------------------------------------------------------ BoostSensorMonitor
namespace {
class BoostSensorMonitorImpl : public detail::Monitor<BoostSensorMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 400;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 100 || raw_ > 3500; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
BoostSensorMonitorImpl g_BoostSensorMonitor;
}  // namespace

MonitorState monitor_BoostSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_BoostSensorMonitor.set(value, reference);
    return g_BoostSensorMonitor.update(dt_ms);
}

void monitor_BoostSensorMonitor_reset()
{
    g_BoostSensorMonitor.reset();
}

// ------------------------------------------------------------------------------------------ GearPositionMonitor
namespace {
class GearPositionMonitorImpl : public detail::Monitor<GearPositionMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 800;
    static constexpr std::uint16_t kHealMs = 400;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ != rpm_; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
GearPositionMonitorImpl g_GearPositionMonitor;
}  // namespace

MonitorState monitor_GearPositionMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_GearPositionMonitor.set(value, reference);
    return g_GearPositionMonitor.update(dt_ms);
}

void monitor_GearPositionMonitor_reset()
{
    g_GearPositionMonitor.reset();
}

// ------------------------------------------------------------------------------------------ thermal management
ThermalOutputs thermal_management(const ThermalInputs& in)
{
    ThermalOutputs out{};
    const bool warm_up = in.coolant_c < kWarmUpC;
    out.pump_pct = warm_up ? (in.heater_request ? 20 : 0) : static_cast<std::int16_t>(detail::clamp<std::int32_t>(
        30 + (in.coolant_c - kWarmUpC) * 3 + in.load_pct / 4, 30, 100));
    out.split_cooling = warm_up && in.load_pct < 50;
    if (in.coolant_c > kCoolantHotC) {
        out.pump_pct = 100;
        out.split_cooling = false;
        out.derate = true;
    }
    out.grille_open = in.coolant_c > kGrilleOpenC || in.ac_on || in.speed_kph > 130;
    if (in.oil_c > kOilHotC) {
        out.derate = true;
        out.grille_open = true;
    }
    out.after_pump = !in.engine_running && in.turbo_c > kTurboHotC;
    return out;
}

// ------------------------------------------------------------------------------------------ air path model
AirMass air_mass(std::int16_t map_hpa, std::int16_t intake_c, std::int16_t rpm, std::int16_t ve_pct, std::int16_t maf_mg)
{
    AirMass a{};
    if (rpm <= 0) {
        return a;
    }
    const std::int32_t kelvin = 273 + intake_c;
    const std::int32_t speed_density = static_cast<std::int32_t>(map_hpa) * ve_pct * kCylinderCc / (kelvin * 287 / 10);
    a.speed_density_mg = static_cast<std::int16_t>(detail::clamp<std::int32_t>(speed_density, 0, 2000));
    a.maf_mg = maf_mg;
    const std::int32_t diff = detail::abs_value<std::int32_t>(a.speed_density_mg - maf_mg);
    a.plausible = maf_mg == 0 || diff * 100 <= std::max<std::int32_t>(maf_mg, 1) * kAirPlausiblePct;
    a.used_mg = a.plausible && maf_mg > 0 ? maf_mg : a.speed_density_mg;
    return a;
}

// ------------------------------------------------------------------------------------------ torque model
namespace {
constexpr detail::Lut<std::int16_t, 5> kFrictionNm({600, 1500, 3000, 4500, 6500}, {12, 15, 20, 28, 40});
constexpr detail::Lut<std::int16_t, 5> kFrictionColdAdd({-20, 0, 20, 60, 90}, {25, 15, 8, 2, 0});
}  // namespace

std::int16_t indicated_torque(std::int16_t air_mg, std::int16_t advance_x10, std::int16_t optimum_x10)
{
    const std::int32_t base = static_cast<std::int32_t>(air_mg) * 16 / 10;
    const std::int32_t loss = detail::abs_value<std::int32_t>(advance_x10 - optimum_x10);
    const std::int32_t efficiency = 1000 - loss * loss / 40;
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(base * std::max<std::int32_t>(efficiency, 0) / 1000, 0, 600));
}

std::int16_t friction_torque(std::int16_t rpm, std::int16_t oil_c, bool ac_on, bool alternator_high)
{
    std::int32_t f = kFrictionNm(rpm) + kFrictionColdAdd(oil_c);
    if (ac_on) {
        f += 8;
    }
    if (alternator_high) {
        f += 5;
    }
    return static_cast<std::int16_t>(f);
}

std::int16_t brake_torque(std::int16_t indicated, std::int16_t friction)
{
    return static_cast<std::int16_t>(indicated - friction);
}

// ------------------------------------------------------------------------------------------ shift sequencing
ShiftPhase ShiftSequencer::step(bool request, std::int16_t torque_nm, std::int16_t clutch_off_pct, std::int16_t clutch_on_pct,
                                bool gear_engaged, std::uint16_t dt_ms)
{
    phase_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(phase_ms_ + dt_ms, 65535u));
    switch (phase_) {
    case ShiftPhase::Idle:
        if (request) {
            phase_ = ShiftPhase::Preselect;
            phase_ms_ = 0;
        }
        break;
    case ShiftPhase::Preselect:
        if (gear_engaged) {
            phase_ = ShiftPhase::TorqueHandover;
            phase_ms_ = 0;
        } else if (phase_ms_ > kPreselectMs) {
            phase_ = ShiftPhase::Abort;
        }
        break;
    case ShiftPhase::TorqueHandover:
        if (clutch_off_pct <= 5 && clutch_on_pct >= 95) {
            phase_ = ShiftPhase::InertiaPhase;
            phase_ms_ = 0;
        } else if (phase_ms_ > kHandoverMs) {
            phase_ = ShiftPhase::Abort;
        }
        break;
    case ShiftPhase::InertiaPhase:
        if (torque_nm >= 0 && phase_ms_ >= kInertiaMs) {
            phase_ = ShiftPhase::Done;
            ++shifts_;
        } else if (phase_ms_ > kInertiaMs * 4) {
            phase_ = ShiftPhase::Abort;
        }
        break;
    case ShiftPhase::Done:
    case ShiftPhase::Abort:
        if (phase_ == ShiftPhase::Abort) {
            ++aborts_;
        }
        phase_ = ShiftPhase::Idle;
        phase_ms_ = 0;
        break;
    default:
        phase_ = ShiftPhase::Idle;
        break;
    }
    return phase_;
}

std::uint16_t ShiftSequencer::shifts() const
{
    return shifts_;
}

std::uint16_t ShiftSequencer::aborts() const
{
    return aborts_;
}

void ShiftSequencer::reset()
{
    phase_ = ShiftPhase::Idle;
    phase_ms_ = 0;
    shifts_ = 0;
    aborts_ = 0;
}

// ------------------------------------------------------------------------------------------ electric drive mode
EvMode EvManager::step(std::int16_t soc_permille, std::int16_t demand_nm, std::int16_t speed_kph, bool ev_button, bool cold,
                       std::uint16_t dt_ms)
{
    const bool ev_possible = soc_permille > kEvMinSoc && speed_kph < kEvMaxKph && !cold;
    switch (mode_) {
    case EvMode::Hybrid:
        if (ev_possible && (ev_button || demand_nm < kEvMaxDemandNm)) {
            mode_ = EvMode::Electric;
            dwell_ms_ = 0;
        } else if (soc_permille < kChargeSoc) {
            mode_ = EvMode::Charge;
        }
        break;
    case EvMode::Electric:
        dwell_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(dwell_ms_ + dt_ms, 65535u));
        if (!ev_possible || (demand_nm > kEvMaxDemandNm + 30 && dwell_ms_ > kEvMinDwellMs)) {
            mode_ = EvMode::Hybrid;
        }
        break;
    case EvMode::Charge:
        if (soc_permille > kChargeSoc + 100) {
            mode_ = EvMode::Hybrid;
        }
        break;
    default:
        mode_ = EvMode::Hybrid;
        break;
    }
    return mode_;
}

void EvManager::reset()
{
    mode_ = EvMode::Hybrid;
    dwell_ms_ = 0;
}

// ------------------------------------------------------------------------------------------ cylinder deactivation
std::uint8_t CylinderDeactivation::step(std::int16_t rpm, std::int16_t load_pct, std::int16_t gear, std::int16_t coolant_c,
                                        bool knock, std::uint16_t dt_ms)
{
    const bool allowed = detail::all_of(rpm > 1200, rpm < 3500, load_pct < 35, gear >= 3, coolant_c > 70, !knock);
    if (!allowed) {
        stable_ms_ = 0;
        if (active_mask_ != 0) {
            ++transitions_;
        }
        active_mask_ = 0;
        return active_mask_;
    }
    stable_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(stable_ms_ + dt_ms, 65535u));
    if (active_mask_ == 0 && stable_ms_ >= kCdaStableMs) {
        active_mask_ = 0x06;       // cylinders 2 and 3
        ++transitions_;
    }
    return active_mask_;
}

std::uint16_t CylinderDeactivation::transitions() const
{
    return transitions_;
}

void CylinderDeactivation::reset()
{
    active_mask_ = 0;
    stable_ms_ = 0;
    transitions_ = 0;
}

// ------------------------------------------------------------------------------------------ turbo protection
std::int16_t turbo_protection(std::int16_t turbo_rpm_k, std::int16_t exhaust_c, std::int16_t altitude_m, std::int16_t torque_nm)
{
    std::int32_t limit = torque_nm;
    if (exhaust_c > 950) {
        limit = limit * (1050 - std::min<std::int32_t>(exhaust_c, 1050)) / 100;
    }
    if (turbo_rpm_k > 200) {
        limit -= (turbo_rpm_k - 200) * 3;
    }
    if (altitude_m > 2000) {
        limit = limit * (100 - (std::min<std::int32_t>(altitude_m, 4000) - 2000) / 100) / 100;
    }
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(limit, 0, torque_nm));
}

// ------------------------------------------------------------------------------------------ vehicle speed
std::int16_t vehicle_speed(const std::array<std::int16_t, 4>& wheels_x10, bool front_driven)
{
    std::array<std::int16_t, 4> sorted = wheels_x10;
    std::sort(sorted.begin(), sorted.end());
    const auto non_driven_lo = front_driven ? std::min(wheels_x10[2], wheels_x10[3]) : std::min(wheels_x10[0], wheels_x10[1]);
    const auto non_driven_hi = front_driven ? std::max(wheels_x10[2], wheels_x10[3]) : std::max(wheels_x10[0], wheels_x10[1]);
    if (non_driven_hi - non_driven_lo > 50) {
        return static_cast<std::int16_t>((sorted[1] + sorted[2]) / 2);
    }
    return static_cast<std::int16_t>((non_driven_lo + non_driven_hi) / 2);
}

bool gear_ratio_plausible(std::int16_t gear, std::int16_t engine_rpm, std::int16_t output_rpm)
{
    static constexpr std::array<std::int16_t, 7> kRatioX100{0, 380, 220, 150, 112, 89, 72};
    if (gear < 1 || gear > 6 || output_rpm < 100) {
        return true;
    }
    const std::int32_t expected = static_cast<std::int32_t>(output_rpm) * kRatioX100[static_cast<std::size_t>(gear)] / 100;
    return detail::abs_value<std::int32_t>(engine_rpm - expected) * 100 <= expected * 8;
}

// ------------------------------------------------------------------------------------------ actuators
std::uint8_t exhaust_flap(DriveMode mode, std::int16_t rpm, bool night_quiet)
{
    if (night_quiet) {
        return 0;
    }
    if (mode == DriveMode::Sport) {
        return rpm > 2500 ? 100 : 60;
    }
    return rpm > 4000 ? 100 : 0;
}

std::uint8_t coolant_pump_pwm(std::int16_t demand_pct, std::int32_t battery_mv)
{
    std::int32_t duty = demand_pct;
    if (battery_mv > 0 && battery_mv != 13500) {
        duty = duty * 13500 / battery_mv;
    }
    return static_cast<std::uint8_t>(detail::clamp<std::int32_t>(duty, 0, 100));
}

// ------------------------------------------------------------------------------------------ diagnostic services
namespace {
struct FreezeFrame {
    std::uint16_t dtc;
    std::int16_t rpm;
    std::int16_t coolant_c;
    std::int16_t speed_kph;
};
detail::RingBuffer<FreezeFrame, 8> g_freeze;
DiagSession g_session = DiagSession::Default;
bool g_security = false;
std::uint16_t g_seed = 0;
std::uint8_t g_attempts = 0;

std::uint16_t key_of(std::uint16_t seed)
{
    std::uint16_t k = static_cast<std::uint16_t>(seed * 13u + 0x4D2u);
    return static_cast<std::uint16_t>((k << 4) | (k >> 12));
}
}  // namespace

bool store_freeze_frame(std::uint16_t dtc, std::int16_t rpm, std::int16_t coolant_c, std::int16_t speed_kph)
{
    if (g_freeze.count_if([dtc](const FreezeFrame& f) { return f.dtc == dtc; }) > 0) {
        return false;
    }
    g_freeze.push(FreezeFrame{dtc, rpm, coolant_c, speed_kph});
    return true;
}

std::size_t freeze_frames()
{
    return g_freeze.size();
}

DiagResponse diag_request(const DiagRequest& req)
{
    DiagResponse r{};
    switch (req.service) {
    case 0x10:
        if (req.sub == 2 && req.speed_kph > 0) {
            r.nrc = 0x22;
        } else if (req.sub < 1 || req.sub > 3) {
            r.nrc = 0x12;
        } else {
            g_session = static_cast<DiagSession>(req.sub);
            g_security = false;
            r.positive = true;
        }
        break;
    case 0x27:
        if (g_session == DiagSession::Default) {
            r.nrc = 0x7F;
        } else if (g_attempts >= 3) {
            r.nrc = 0x36;
        } else if (req.sub == 1) {
            g_seed = static_cast<std::uint16_t>(req.data | 1u);
            r.value = g_security ? 0 : g_seed;
            r.positive = true;
        } else if (req.sub == 2) {
            if (g_seed != 0 && req.data == key_of(g_seed)) {
                g_security = true;
                g_attempts = 0;
                r.positive = true;
            } else {
                ++g_attempts;
                r.nrc = 0x35;
            }
            g_seed = 0;
        } else {
            r.nrc = 0x12;
        }
        break;
    case 0x14:
        if (g_session != DiagSession::Extended && g_session != DiagSession::Default) {
            r.nrc = 0x22;
        } else {
            g_freeze.clear();
            r.positive = true;
        }
        break;
    case 0x22:
        if (req.data == 0xF40C) {
            r.value = static_cast<std::uint16_t>(req.speed_kph);
            r.positive = true;
        } else if (req.data == 0xF190) {
            r.value = 0x5054;          // "PT"
            r.positive = true;
        } else {
            r.nrc = 0x31;
        }
        break;
    case 0x2E:
        if (!g_security) {
            r.nrc = 0x33;
        } else if (req.data > 0x7FFF) {
            r.nrc = 0x31;
        } else {
            r.positive = limit_param_set(0, static_cast<std::int16_t>(req.data));
            r.nrc = r.positive ? 0 : 0x31;
        }
        break;
    default:
        r.nrc = 0x11;
        break;
    }
    return r;
}

DiagSession diag_session()
{
    return g_session;
}

void diag_reset()
{
    g_freeze.clear();
    g_session = DiagSession::Default;
    g_security = false;
    g_seed = 0;
    g_attempts = 0;
}

std::uint16_t diag_key_for(std::uint16_t seed)
{
    return key_of(seed);
}

// ------------------------------------------------------------------------------------------ brake temperature fl
namespace {
std::int32_t g_brake_fl_c = 20;
}  // namespace

std::int16_t brake_temp_fl(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms)
{
    const std::int32_t heat = static_cast<std::int32_t>(brake_nm) * wheel_kph / 2000;
    const std::int32_t cooling = (g_brake_fl_c - ambient_c) * (3 + wheel_kph / 40) / 100;
    g_brake_fl_c += (heat - cooling) * dt_ms / 100;
    g_brake_fl_c = detail::clamp<std::int32_t>(g_brake_fl_c, ambient_c, 900);
    return static_cast<std::int16_t>(g_brake_fl_c);
}

bool brake_fade_fl()
{
    return g_brake_fl_c > kBrakeFadeC;
}

void brake_temp_fl_reset(std::int16_t ambient_c)
{
    g_brake_fl_c = ambient_c;
}

// ------------------------------------------------------------------------------------------ brake temperature fr
namespace {
std::int32_t g_brake_fr_c = 20;
}  // namespace

std::int16_t brake_temp_fr(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms)
{
    const std::int32_t heat = static_cast<std::int32_t>(brake_nm) * wheel_kph / 2000;
    const std::int32_t cooling = (g_brake_fr_c - ambient_c) * (3 + wheel_kph / 40) / 100;
    g_brake_fr_c += (heat - cooling) * dt_ms / 100;
    g_brake_fr_c = detail::clamp<std::int32_t>(g_brake_fr_c, ambient_c, 900);
    return static_cast<std::int16_t>(g_brake_fr_c);
}

bool brake_fade_fr()
{
    return g_brake_fr_c > kBrakeFadeC;
}

void brake_temp_fr_reset(std::int16_t ambient_c)
{
    g_brake_fr_c = ambient_c;
}

// ------------------------------------------------------------------------------------------ brake temperature rl
namespace {
std::int32_t g_brake_rl_c = 20;
}  // namespace

std::int16_t brake_temp_rl(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms)
{
    const std::int32_t heat = static_cast<std::int32_t>(brake_nm) * wheel_kph / 2000;
    const std::int32_t cooling = (g_brake_rl_c - ambient_c) * (2 + wheel_kph / 40) / 100;
    g_brake_rl_c += (heat - cooling) * dt_ms / 100;
    g_brake_rl_c = detail::clamp<std::int32_t>(g_brake_rl_c, ambient_c, 900);
    return static_cast<std::int16_t>(g_brake_rl_c);
}

bool brake_fade_rl()
{
    return g_brake_rl_c > kBrakeFadeC;
}

void brake_temp_rl_reset(std::int16_t ambient_c)
{
    g_brake_rl_c = ambient_c;
}

// ------------------------------------------------------------------------------------------ brake temperature rr
namespace {
std::int32_t g_brake_rr_c = 20;
}  // namespace

std::int16_t brake_temp_rr(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms)
{
    const std::int32_t heat = static_cast<std::int32_t>(brake_nm) * wheel_kph / 2000;
    const std::int32_t cooling = (g_brake_rr_c - ambient_c) * (2 + wheel_kph / 40) / 100;
    g_brake_rr_c += (heat - cooling) * dt_ms / 100;
    g_brake_rr_c = detail::clamp<std::int32_t>(g_brake_rr_c, ambient_c, 900);
    return static_cast<std::int16_t>(g_brake_rr_c);
}

bool brake_fade_rr()
{
    return g_brake_rr_c > kBrakeFadeC;
}

void brake_temp_rr_reset(std::int16_t ambient_c)
{
    g_brake_rr_c = ambient_c;
}

// ------------------------------------------------------------------------------------------ knock window 1
namespace {
detail::Ema<std::int16_t, 3> g_noise_1;
}  // namespace

KnockWindow knock_window_1(std::int16_t rpm, std::uint16_t integral, bool injection_noise)
{
    KnockWindow w{};
    w.start_deg = static_cast<std::int16_t>(rpm > 4000 ? 8 : 10);
    w.length_deg = static_cast<std::int16_t>(rpm > 4000 ? 50 : 40);
    if (injection_noise) {
        w.valid = false;
        return w;
    }
    const std::int16_t noise = g_noise_1.update(static_cast<std::int16_t>(std::min<std::uint32_t>(integral, 32767u)));
    w.threshold = static_cast<std::uint16_t>(noise * 3 / 2 + 50);
    w.knock = integral > w.threshold && g_noise_1.primed();
    w.valid = true;
    return w;
}

void knock_window_1_reset()
{
    g_noise_1.reset();
}

// ------------------------------------------------------------------------------------------ knock window 2
namespace {
detail::Ema<std::int16_t, 3> g_noise_2;
}  // namespace

KnockWindow knock_window_2(std::int16_t rpm, std::uint16_t integral, bool injection_noise)
{
    KnockWindow w{};
    w.start_deg = static_cast<std::int16_t>(rpm > 4000 ? 8 : 10);
    w.length_deg = static_cast<std::int16_t>(rpm > 4000 ? 50 : 40);
    if (injection_noise) {
        w.valid = false;
        return w;
    }
    const std::int16_t noise = g_noise_2.update(static_cast<std::int16_t>(std::min<std::uint32_t>(integral, 32767u)));
    w.threshold = static_cast<std::uint16_t>(noise * 3 / 2 + 50);
    w.knock = integral > w.threshold && g_noise_2.primed();
    w.valid = true;
    return w;
}

void knock_window_2_reset()
{
    g_noise_2.reset();
}

// ------------------------------------------------------------------------------------------ knock window 3
namespace {
detail::Ema<std::int16_t, 3> g_noise_3;
}  // namespace

KnockWindow knock_window_3(std::int16_t rpm, std::uint16_t integral, bool injection_noise)
{
    KnockWindow w{};
    w.start_deg = static_cast<std::int16_t>(rpm > 4000 ? 8 : 10);
    w.length_deg = static_cast<std::int16_t>(rpm > 4000 ? 50 : 40);
    if (injection_noise) {
        w.valid = false;
        return w;
    }
    const std::int16_t noise = g_noise_3.update(static_cast<std::int16_t>(std::min<std::uint32_t>(integral, 32767u)));
    w.threshold = static_cast<std::uint16_t>(noise * 3 / 2 + 50);
    w.knock = integral > w.threshold && g_noise_3.primed();
    w.valid = true;
    return w;
}

void knock_window_3_reset()
{
    g_noise_3.reset();
}

// ------------------------------------------------------------------------------------------ knock window 4
namespace {
detail::Ema<std::int16_t, 3> g_noise_4;
}  // namespace

KnockWindow knock_window_4(std::int16_t rpm, std::uint16_t integral, bool injection_noise)
{
    KnockWindow w{};
    w.start_deg = static_cast<std::int16_t>(rpm > 4000 ? 8 : 10);
    w.length_deg = static_cast<std::int16_t>(rpm > 4000 ? 50 : 40);
    if (injection_noise) {
        w.valid = false;
        return w;
    }
    const std::int16_t noise = g_noise_4.update(static_cast<std::int16_t>(std::min<std::uint32_t>(integral, 32767u)));
    w.threshold = static_cast<std::uint16_t>(noise * 3 / 2 + 50);
    w.knock = integral > w.threshold && g_noise_4.primed();
    w.valid = true;
    return w;
}

void knock_window_4_reset()
{
    g_noise_4.reset();
}

// ------------------------------------------------------------------------------------------ control loops by traits
namespace {
struct OilPumpTraits {
    static constexpr std::int32_t kp = 150, ki = 6, kd = 0;
    static constexpr std::int16_t lo = 0, hi = 100;
    static constexpr std::int16_t min_target = 10, max_target = 50;
};
struct CoolantValveTraits {
    static constexpr std::int32_t kp = 90, ki = 3, kd = 10;
    static constexpr std::int16_t lo = 0, hi = 100;
    static constexpr std::int16_t min_target = 80, max_target = 105;
};
struct GrilleTraits {
    static constexpr std::int32_t kp = 60, ki = 2, kd = 0;
    static constexpr std::int16_t lo = 0, hi = 100;
    static constexpr std::int16_t min_target = 0, max_target = 100;
};
struct BypassTraits {
    static constexpr std::int32_t kp = 200, ki = 10, kd = 30;
    static constexpr std::int16_t lo = -50, hi = 50;
    static constexpr std::int16_t min_target = -20, max_target = 20;
};

template <typename Traits>
class ControlLoop {
public:
    std::int16_t step(std::int16_t target, std::int16_t measured, bool enabled)
    {
        if (!enabled) {
            pid_.reset();
            return Traits::lo;
        }
        const std::int16_t t = detail::clamp<std::int16_t>(target, Traits::min_target, Traits::max_target);
        return pid_.step(t, measured, false);
    }
    void reset() { pid_.reset(); }

private:
    detail::Pid<std::int16_t> pid_{Traits::kp, Traits::ki, Traits::kd, Traits::lo, Traits::hi};
};

ControlLoop<OilPumpTraits> g_oil_pump;
ControlLoop<CoolantValveTraits> g_coolant_valve;
ControlLoop<GrilleTraits> g_grille;
ControlLoop<BypassTraits> g_bypass;
}  // namespace

std::int16_t oil_pump_control(std::int16_t target_bar_x10, std::int16_t measured_bar_x10, bool engine_running)
{
    return g_oil_pump.step(target_bar_x10, measured_bar_x10, engine_running);
}

std::int16_t coolant_valve_control(std::int16_t target_c, std::int16_t measured_c, bool engine_running)
{
    return g_coolant_valve.step(target_c, measured_c, engine_running);
}

std::int16_t grille_control(std::int16_t target_pct, std::int16_t measured_pct, bool enabled)
{
    return g_grille.step(target_pct, measured_pct, enabled);
}

std::int16_t bypass_control(std::int16_t target, std::int16_t measured, bool idle)
{
    return g_bypass.step(target, measured, idle);
}

void control_loops_reset()
{
    g_oil_pump.reset();
    g_coolant_valve.reset();
    g_grille.reset();
    g_bypass.reset();
}

// ------------------------------------------------------------------------------------------ engine run state
EngineRun EngineRunState::step(bool key_on, bool start_request, std::int16_t rpm, bool fuel_cut_request, std::uint16_t dt_ms)
{
    switch (state_) {
    case EngineRun::Off:
        if (key_on && start_request) {
            state_ = EngineRun::Cranking;
            time_ms_ = 0;
        }
        break;
    case EngineRun::Cranking:
        time_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(time_ms_ + dt_ms, 65535u));
        if (rpm > kRunRpm) {
            state_ = EngineRun::Running;
            ++starts_;
        } else if (!start_request || time_ms_ > kCrankTimeoutMs) {
            state_ = EngineRun::Off;
            ++failed_starts_;
        }
        break;
    case EngineRun::Running:
        if (!key_on || fuel_cut_request) {
            state_ = EngineRun::Afterrun;
            time_ms_ = 0;
        } else if (rpm < kStallRpm) {
            state_ = EngineRun::Stalled;
            ++stalls_;
        }
        break;
    case EngineRun::Stalled:
        if (!key_on) {
            state_ = EngineRun::Off;
        } else if (start_request) {
            state_ = EngineRun::Cranking;
            time_ms_ = 0;
        }
        break;
    case EngineRun::Afterrun:
        time_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(time_ms_ + dt_ms, 65535u));
        if (key_on && start_request) {
            state_ = EngineRun::Cranking;
            time_ms_ = 0;
        } else if (time_ms_ > kAfterrunMs) {
            state_ = EngineRun::Off;
        }
        break;
    default:
        state_ = EngineRun::Off;
        break;
    }
    return state_;
}

std::uint16_t EngineRunState::starts() const
{
    return starts_;
}

std::uint16_t EngineRunState::failed_starts() const
{
    return failed_starts_;
}

std::uint16_t EngineRunState::stalls() const
{
    return stalls_;
}

void EngineRunState::reset()
{
    state_ = EngineRun::Off;
    time_ms_ = 0;
    starts_ = 0;
    failed_starts_ = 0;
    stalls_ = 0;
}

// ------------------------------------------------------------------------------------------ high voltage system
HvState HighVoltage::step(const HvInputs& in, std::uint16_t dt_ms)
{
    if (in.crash || !in.interlock_closed) {
        state_ = HvState::Discharge;
        time_ms_ = 0;
    }
    if (in.isolation_kohm < kIsolationFaultKohm) {
        isolation_fault_ = true;
    } else if (in.isolation_kohm > kIsolationWarnKohm) {
        isolation_fault_ = false;
    }
    switch (state_) {
    case HvState::Off:
        if (in.request_on && in.interlock_closed && !isolation_fault_) {
            state_ = HvState::Precharge;
            time_ms_ = 0;
        }
        break;
    case HvState::Precharge:
        time_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(time_ms_ + dt_ms, 65535u));
        if (in.link_mv * 100 >= in.battery_mv * kPrechargePct) {
            state_ = HvState::On;
        } else if (time_ms_ > kPrechargeTimeoutMs) {
            state_ = HvState::Fault;
        }
        break;
    case HvState::On:
        if (!in.request_on || isolation_fault_) {
            state_ = HvState::Discharge;
            time_ms_ = 0;
        }
        break;
    case HvState::Discharge:
        time_ms_ = static_cast<std::uint16_t>(std::min<std::uint32_t>(time_ms_ + dt_ms, 65535u));
        if (in.link_mv < kSafeLinkMv) {
            state_ = HvState::Off;
        } else if (time_ms_ > kDischargeTimeoutMs) {
            state_ = HvState::Fault;
        }
        break;
    case HvState::Fault:
        if (!in.request_on && in.link_mv < kSafeLinkMv && in.interlock_closed && !in.crash) {
            state_ = HvState::Off;
        }
        break;
    default:
        state_ = HvState::Fault;
        break;
    }
    return state_;
}

bool HighVoltage::isolation_fault() const
{
    return isolation_fault_;
}

void HighVoltage::reset()
{
    state_ = HvState::Off;
    time_ms_ = 0;
    isolation_fault_ = false;
}

// ------------------------------------------------------------------------------------------ charging
ChargeState Charger::step(bool plug_locked, bool proximity, std::int16_t pilot_pct, std::int16_t soc_permille, std::int16_t battery_c,
                          bool user_stop)
{
    if (!proximity) {
        state_ = ChargeState::Unplugged;
        current_limit_a_ = 0;
        return state_;
    }
    const std::int16_t pilot_a = pilot_pct >= 10 && pilot_pct <= 85 ? static_cast<std::int16_t>(pilot_pct * 6 / 10) : 0;
    switch (state_) {
    case ChargeState::Unplugged:
        state_ = ChargeState::Connected;
        break;
    case ChargeState::Connected:
        if (plug_locked && pilot_a > 0 && soc_permille < kChargeTargetSoc && !user_stop) {
            state_ = ChargeState::Charging;
        }
        break;
    case ChargeState::Charging:
        if (user_stop || !plug_locked || pilot_a == 0) {
            state_ = ChargeState::Connected;
        } else if (soc_permille >= kChargeTargetSoc) {
            state_ = ChargeState::Complete;
        } else if (battery_c > kChargeMaxC || battery_c < kChargeMinC) {
            state_ = ChargeState::Paused;
        }
        break;
    case ChargeState::Paused:
        if (battery_c <= kChargeMaxC - 5 && battery_c >= kChargeMinC + 5) {
            state_ = ChargeState::Charging;
        } else if (user_stop) {
            state_ = ChargeState::Connected;
        }
        break;
    case ChargeState::Complete:
        if (soc_permille < kChargeTargetSoc - 50) {
            state_ = ChargeState::Connected;
        }
        break;
    default:
        state_ = ChargeState::Connected;
        break;
    }
    current_limit_a_ = state_ == ChargeState::Charging ? std::min<std::int16_t>(pilot_a, kChargerMaxA) : 0;
    if (state_ == ChargeState::Charging && soc_permille > kChargeTargetSoc - 100) {
        current_limit_a_ = static_cast<std::int16_t>(current_limit_a_ / 2);
    }
    return state_;
}

std::int16_t Charger::current_limit() const
{
    return current_limit_a_;
}

void Charger::reset()
{
    state_ = ChargeState::Unplugged;
    current_limit_a_ = 0;
}

// ------------------------------------------------------------------------------------------ DC/DC converter
std::int16_t dcdc_target_mv(HvState hv, std::int16_t lv_battery_c, std::int16_t lv_soc_pct, bool high_load)
{
    if (hv != HvState::On) {
        return 0;
    }
    std::int32_t mv = 14000 - (lv_battery_c - 25) * 8;
    if (lv_soc_pct < 70 || high_load) {
        mv += 400;
    }
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(mv, 13000, 14800));
}

// ------------------------------------------------------------------------------------------ battery thermal
BatteryThermal battery_thermal(std::int16_t cell_max_c, std::int16_t cell_min_c, bool charging, bool driving)
{
    BatteryThermal t{};
    if (cell_max_c > 40 || (charging && cell_max_c > 35)) {
        t.cooling = true;
        t.chiller = cell_max_c > 45;
    }
    if (cell_min_c < (charging ? 10 : 0)) {
        t.heater = true;
    }
    t.power_limit_pct = 100;
    if (cell_max_c > 55 || cell_min_c < -20) {
        t.power_limit_pct = 30;
    } else if (cell_max_c > 50 || cell_min_c < -10) {
        t.power_limit_pct = 60;
    }
    if (cell_max_c - cell_min_c > 10 && driving) {
        t.power_limit_pct = static_cast<std::int16_t>(std::min<std::int32_t>(t.power_limit_pct, 80));
    }
    return t;
}

// ------------------------------------------------------------------------------------------ shift recommendation
std::int16_t shift_recommendation(std::int16_t gear, std::int16_t rpm, std::int16_t pedal_pct, bool manual_mode)
{
    if (!manual_mode) {
        return 0;
    }
    if (rpm > 3000 && pedal_pct < 50 && gear < 6) {
        return 1;
    }
    if (rpm < 1200 && gear > 1) {
        return -1;
    }
    return 0;
}

// ------------------------------------------------------------------------------------------ trailer detection
bool trailer_detected(std::int16_t mass_estimate_kg, std::int16_t curb_kg, bool trailer_socket, std::int16_t lamp_current_ma)
{
    if (trailer_socket && lamp_current_ma > 300) {
        return true;
    }
    return mass_estimate_kg > curb_kg + 900;
}

// ------------------------------------------------------------------------------------------ CAN message thermal
namespace {
using ThermalSigCoolant = detail::Signal<0, 8, 1, -40, -40, 215>;
using ThermalSigOil = detail::Signal<8, 8, 1, -40, -40, 215>;
using ThermalSigPump = detail::Signal<16, 7, 1, 0, 0, 100>;
using ThermalSigGrille = detail::Signal<23, 1, 1, 0, 0, 1>;
using ThermalSigDerate = detail::Signal<24, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_thermal(const ThermalMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return ThermalSigCoolant::encode(frame, m.coolant) &&
           ThermalSigOil::encode(frame, m.oil) &&
           ThermalSigPump::encode(frame, m.pump) &&
           ThermalSigGrille::encode(frame, m.grille) &&
           ThermalSigDerate::encode(frame, m.derate);
}

ThermalMsg unpack_thermal(const std::array<std::uint8_t, 8>& frame)
{
    ThermalMsg m{};
    m.coolant = ThermalSigCoolant::decode(frame);
    m.oil = ThermalSigOil::decode(frame);
    m.pump = ThermalSigPump::decode(frame);
    m.grille = ThermalSigGrille::decode(frame);
    m.derate = ThermalSigDerate::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message hv_status
namespace {
using HvStatusSigState = detail::Signal<0, 3, 1, 0, 0, 4>;
using HvStatusSigLinkV = detail::Signal<3, 10, 1, 0, 0, 1000>;
using HvStatusSigIsolation = detail::Signal<13, 12, 1, 0, 0, 4000>;
using HvStatusSigInterlock = detail::Signal<25, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_hv_status(const HvStatusMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return HvStatusSigState::encode(frame, m.state) &&
           HvStatusSigLinkV::encode(frame, m.linkV) &&
           HvStatusSigIsolation::encode(frame, m.isolation) &&
           HvStatusSigInterlock::encode(frame, m.interlock);
}

HvStatusMsg unpack_hv_status(const std::array<std::uint8_t, 8>& frame)
{
    HvStatusMsg m{};
    m.state = HvStatusSigState::decode(frame);
    m.linkV = HvStatusSigLinkV::decode(frame);
    m.isolation = HvStatusSigIsolation::decode(frame);
    m.interlock = HvStatusSigInterlock::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message charging
namespace {
using ChargingSigChgState = detail::Signal<0, 3, 1, 0, 0, 4>;
using ChargingSigCurrentA = detail::Signal<3, 7, 1, 0, 0, 100>;
using ChargingSigSoc = detail::Signal<10, 10, 1, 0, 0, 1000>;
using ChargingSigPlugLocked = detail::Signal<20, 1, 1, 0, 0, 1>;
}  // namespace

bool pack_charging(const ChargingMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return ChargingSigChgState::encode(frame, m.chgState) &&
           ChargingSigCurrentA::encode(frame, m.currentA) &&
           ChargingSigSoc::encode(frame, m.soc) &&
           ChargingSigPlugLocked::encode(frame, m.plugLocked);
}

ChargingMsg unpack_charging(const std::array<std::uint8_t, 8>& frame)
{
    ChargingMsg m{};
    m.chgState = ChargingSigChgState::decode(frame);
    m.currentA = ChargingSigCurrentA::decode(frame);
    m.soc = ChargingSigSoc::decode(frame);
    m.plugLocked = ChargingSigPlugLocked::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message diagnosis
namespace {
using DiagnosisSigMil = detail::Signal<0, 1, 1, 0, 0, 1>;
using DiagnosisSigDtcCount = detail::Signal<1, 8, 1, 0, 0, 255>;
using DiagnosisSigReady = detail::Signal<9, 8, 1, 0, 0, 255>;
using DiagnosisSigMisfire = detail::Signal<17, 4, 1, 0, 0, 15>;
}  // namespace

bool pack_diagnosis(const DiagnosisMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return DiagnosisSigMil::encode(frame, m.mil) &&
           DiagnosisSigDtcCount::encode(frame, m.dtcCount) &&
           DiagnosisSigReady::encode(frame, m.ready) &&
           DiagnosisSigMisfire::encode(frame, m.misfire);
}

DiagnosisMsg unpack_diagnosis(const std::array<std::uint8_t, 8>& frame)
{
    DiagnosisMsg m{};
    m.mil = DiagnosisSigMil::decode(frame);
    m.dtcCount = DiagnosisSigDtcCount::decode(frame);
    m.ready = DiagnosisSigReady::decode(frame);
    m.misfire = DiagnosisSigMisfire::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message engine_temps
namespace {
using EngineTempsSigExhaust = detail::Signal<0, 11, 1, 0, 0, 1200>;
using EngineTempsSigTurbo = detail::Signal<11, 11, 1, 0, 0, 1200>;
using EngineTempsSigIntake = detail::Signal<22, 8, 1, -40, -40, 215>;
using EngineTempsSigCatalyst = detail::Signal<30, 11, 1, 0, 0, 1200>;
}  // namespace

bool pack_engine_temps(const EngineTempsMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return EngineTempsSigExhaust::encode(frame, m.exhaust) &&
           EngineTempsSigTurbo::encode(frame, m.turbo) &&
           EngineTempsSigIntake::encode(frame, m.intake) &&
           EngineTempsSigCatalyst::encode(frame, m.catalyst);
}

EngineTempsMsg unpack_engine_temps(const std::array<std::uint8_t, 8>& frame)
{
    EngineTempsMsg m{};
    m.exhaust = EngineTempsSigExhaust::decode(frame);
    m.turbo = EngineTempsSigTurbo::decode(frame);
    m.intake = EngineTempsSigIntake::decode(frame);
    m.catalyst = EngineTempsSigCatalyst::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message driver_info
namespace {
using DriverInfoSigRange = detail::Signal<0, 11, 1, 0, 0, 2000>;
using DriverInfoSigConsumption = detail::Signal<11, 10, 1, 0, 0, 1000>;
using DriverInfoSigService = detail::Signal<21, 2, 1, 0, 0, 2>;
using DriverInfoSigShiftHint = detail::Signal<23, 2, 1, -1, -1, 1>;
}  // namespace

bool pack_driver_info(const DriverInfoMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return DriverInfoSigRange::encode(frame, m.range) &&
           DriverInfoSigConsumption::encode(frame, m.consumption) &&
           DriverInfoSigService::encode(frame, m.service) &&
           DriverInfoSigShiftHint::encode(frame, m.shiftHint);
}

DriverInfoMsg unpack_driver_info(const std::array<std::uint8_t, 8>& frame)
{
    DriverInfoMsg m{};
    m.range = DriverInfoSigRange::decode(frame);
    m.consumption = DriverInfoSigConsumption::decode(frame);
    m.service = DriverInfoSigService::decode(frame);
    m.shiftHint = DriverInfoSigShiftHint::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ FuelLevelMonitor
namespace {
class FuelLevelMonitorImpl : public detail::Monitor<FuelLevelMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 3000;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 5 || raw_ > 250; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
FuelLevelMonitorImpl g_FuelLevelMonitor;
}  // namespace

MonitorState monitor_FuelLevelMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_FuelLevelMonitor.set(value, reference);
    return g_FuelLevelMonitor.update(dt_ms);
}

void monitor_FuelLevelMonitor_reset()
{
    g_FuelLevelMonitor.reset();
}

// ------------------------------------------------------------------------------------------ ExhaustTempMonitor
namespace {
class ExhaustTempMonitorImpl : public detail::Monitor<ExhaustTempMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 500;
    static constexpr std::uint16_t kHealMs = 2000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ > 1050; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
ExhaustTempMonitorImpl g_ExhaustTempMonitor;
}  // namespace

MonitorState monitor_ExhaustTempMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_ExhaustTempMonitor.set(value, reference);
    return g_ExhaustTempMonitor.update(dt_ms);
}

void monitor_ExhaustTempMonitor_reset()
{
    g_ExhaustTempMonitor.reset();
}

// ------------------------------------------------------------------------------------------ TransOilMonitor
namespace {
class TransOilMonitorImpl : public detail::Monitor<TransOilMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 2000;
    static constexpr std::uint16_t kHealMs = 5000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ > 140 && rpm_ > 0; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
TransOilMonitorImpl g_TransOilMonitor;
}  // namespace

MonitorState monitor_TransOilMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_TransOilMonitor.set(value, reference);
    return g_TransOilMonitor.update(dt_ms);
}

void monitor_TransOilMonitor_reset()
{
    g_TransOilMonitor.reset();
}

// ------------------------------------------------------------------------------------------ HvIsolationMonitor
namespace {
class HvIsolationMonitorImpl : public detail::Monitor<HvIsolationMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 100;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 100; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
HvIsolationMonitorImpl g_HvIsolationMonitor;
}  // namespace

MonitorState monitor_HvIsolationMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_HvIsolationMonitor.set(value, reference);
    return g_HvIsolationMonitor.update(dt_ms);
}

void monitor_HvIsolationMonitor_reset()
{
    g_HvIsolationMonitor.reset();
}

// ------------------------------------------------------------------------------------------ CellVoltageMonitor
namespace {
class CellVoltageMonitorImpl : public detail::Monitor<CellVoltageMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 200;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 2800 || raw_ > 4250; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
CellVoltageMonitorImpl g_CellVoltageMonitor;
}  // namespace

MonitorState monitor_CellVoltageMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_CellVoltageMonitor.set(value, reference);
    return g_CellVoltageMonitor.update(dt_ms);
}

void monitor_CellVoltageMonitor_reset()
{
    g_CellVoltageMonitor.reset();
}

// ------------------------------------------------------------------------------------------ ChargerPilotMonitor
namespace {
class ChargerPilotMonitorImpl : public detail::Monitor<ChargerPilotMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 300;
    static constexpr std::uint16_t kHealMs = 300;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ != 0 && (raw_ < 8 || raw_ > 97); }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
ChargerPilotMonitorImpl g_ChargerPilotMonitor;
}  // namespace

MonitorState monitor_ChargerPilotMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_ChargerPilotMonitor.set(value, reference);
    return g_ChargerPilotMonitor.update(dt_ms);
}

void monitor_ChargerPilotMonitor_reset()
{
    g_ChargerPilotMonitor.reset();
}

// ------------------------------------------------------------------------------------------ LambdaSensorMonitor
namespace {
class LambdaSensorMonitorImpl : public detail::Monitor<LambdaSensorMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 1500;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 50 || raw_ > 950; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
LambdaSensorMonitorImpl g_LambdaSensorMonitor;
}  // namespace

MonitorState monitor_LambdaSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_LambdaSensorMonitor.set(value, reference);
    return g_LambdaSensorMonitor.update(dt_ms);
}

void monitor_LambdaSensorMonitor_reset()
{
    g_LambdaSensorMonitor.reset();
}

// ------------------------------------------------------------------------------------------ KnockSensorMonitor
namespace {
class KnockSensorMonitorImpl : public detail::Monitor<KnockSensorMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 2000;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 5 && rpm_ > 2000; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
KnockSensorMonitorImpl g_KnockSensorMonitor;
}  // namespace

MonitorState monitor_KnockSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_KnockSensorMonitor.set(value, reference);
    return g_KnockSensorMonitor.update(dt_ms);
}

void monitor_KnockSensorMonitor_reset()
{
    g_KnockSensorMonitor.reset();
}

// ------------------------------------------------------------------------------------------ WheelSpeedMonitor
namespace {
class WheelSpeedMonitorImpl : public detail::Monitor<WheelSpeedMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 1000;
    static constexpr std::uint16_t kHealMs = 500;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return detail::abs_value<std::int16_t>(static_cast<std::int16_t>(raw_ - rpm_)) > 100; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
WheelSpeedMonitorImpl g_WheelSpeedMonitor;
}  // namespace

MonitorState monitor_WheelSpeedMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_WheelSpeedMonitor.set(value, reference);
    return g_WheelSpeedMonitor.update(dt_ms);
}

void monitor_WheelSpeedMonitor_reset()
{
    g_WheelSpeedMonitor.reset();
}

// ------------------------------------------------------------------------------------------ ThrottleSpringMonitor
namespace {
class ThrottleSpringMonitorImpl : public detail::Monitor<ThrottleSpringMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 300;
    static constexpr std::uint16_t kHealMs = 1000;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ > 12 && rpm_ == 0; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
ThrottleSpringMonitorImpl g_ThrottleSpringMonitor;
}  // namespace

MonitorState monitor_ThrottleSpringMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_ThrottleSpringMonitor.set(value, reference);
    return g_ThrottleSpringMonitor.update(dt_ms);
}

void monitor_ThrottleSpringMonitor_reset()
{
    g_ThrottleSpringMonitor.reset();
}

// ------------------------------------------------------------------------------------------ AltitudeMonitor
namespace {
class AltitudeMonitorImpl : public detail::Monitor<AltitudeMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 5000;
    static constexpr std::uint16_t kHealMs = 2000;
    static constexpr bool kLatching = false;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ < 500 || raw_ > 1100; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
AltitudeMonitorImpl g_AltitudeMonitor;
}  // namespace

MonitorState monitor_AltitudeMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_AltitudeMonitor.set(value, reference);
    return g_AltitudeMonitor.update(dt_ms);
}

void monitor_AltitudeMonitor_reset()
{
    g_AltitudeMonitor.reset();
}

// ------------------------------------------------------------------------------------------ ContactorMonitor
namespace {
class ContactorMonitorImpl : public detail::Monitor<ContactorMonitorImpl> {
public:
    static constexpr std::uint16_t kFailMs = 100;
    static constexpr std::uint16_t kHealMs = 200;
    static constexpr bool kLatching = true;
    void set(std::int16_t raw, std::int16_t rpm) { raw_ = raw; rpm_ = rpm; }
    bool check() const { return raw_ != rpm_; }

private:
    std::int16_t raw_ = 0;
    std::int16_t rpm_ = 0;
};
ContactorMonitorImpl g_ContactorMonitor;
}  // namespace

MonitorState monitor_ContactorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms)
{
    g_ContactorMonitor.set(value, reference);
    return g_ContactorMonitor.update(dt_ms);
}

void monitor_ContactorMonitor_reset()
{
    g_ContactorMonitor.reset();
}

// ------------------------------------------------------------------------------------------ following distance
std::int16_t FollowControl::step(std::int16_t own_kph, std::int16_t set_kph, bool target_valid, std::int16_t gap_m,
                                 std::int16_t closing_kph, GapSetting setting)
{
    const std::int16_t time_gap_ds = setting == GapSetting::Near ? 10 : (setting == GapSetting::Middle ? 15 : 22);
    const std::int32_t wanted_m = std::max<std::int32_t>(5, own_kph * time_gap_ds / 36);
    if (!target_valid) {
        following_ = false;
        return static_cast<std::int16_t>(detail::clamp<std::int32_t>((set_kph - own_kph) * 10, -200, 150));
    }
    following_ = true;
    const std::int32_t gap_error = gap_m - wanted_m;
    std::int32_t accel = gap_error * 4 - closing_kph * 12;
    if (own_kph >= set_kph && accel > 0) {
        accel = 0;
    }
    if (gap_m < wanted_m / 3) {
        warning_ = true;
        accel = -350;
    } else {
        warning_ = false;
    }
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(accel, -350, 150));
}

bool FollowControl::following() const
{
    return following_;
}

bool FollowControl::warning() const
{
    return warning_;
}

void FollowControl::reset()
{
    following_ = false;
    warning_ = false;
}

// ------------------------------------------------------------------------------------------ immobilizer
bool Immobilizer::challenge(std::uint32_t random)
{
    if (blocked_) {
        return false;
    }
    challenge_ = random == 0 ? 0x2468ACE1u : random;
    released_ = false;
    return true;
}

bool Immobilizer::answer(std::uint32_t response, std::uint32_t secret)
{
    if (challenge_ == 0 || blocked_) {
        return false;
    }
    std::uint32_t expected = challenge_ ^ secret;
    for (int i = 0; i < 3; ++i) {
        expected = (expected * 2654435761u) ^ (expected >> 13);
    }
    challenge_ = 0;
    if (response == expected) {
        released_ = true;
        failures_ = 0;
        return true;
    }
    ++failures_;
    blocked_ = failures_ >= kImmoMaxFailures;
    return false;
}

bool Immobilizer::released() const
{
    return released_;
}

bool Immobilizer::blocked() const
{
    return blocked_;
}

void Immobilizer::reset()
{
    challenge_ = 0;
    released_ = false;
    failures_ = 0;
    blocked_ = false;
}

// ------------------------------------------------------------------------------------------ drive cycles
void DriveCycle::update(std::int16_t coolant_c, std::int16_t speed_kph, std::uint32_t run_ms)
{
    if (!warm_seen_ && coolant_c >= start_coolant_ + 22 && coolant_c >= 70) {
        warm_seen_ = true;
        ++warm_ups_;
    }
    if (speed_kph > 40 && run_ms > 600000u && !cycle_done_) {
        cycle_done_ = true;
        ++cycles_;
    }
}

void DriveCycle::start(std::int16_t coolant_c)
{
    start_coolant_ = coolant_c;
    warm_seen_ = false;
    cycle_done_ = false;
}

std::uint16_t DriveCycle::warm_ups() const
{
    return warm_ups_;
}

std::uint16_t DriveCycle::cycles() const
{
    return cycles_;
}

// ------------------------------------------------------------------------------------------ range estimate
std::int16_t RangeEstimator::update(std::int16_t consumption_x10, std::int16_t fuel_dl)
{
    std::int16_t old = 0;
    if (consumption_.size() == kRangeSamples && consumption_.pop(old)) {
        sum_ -= old;
    }
    consumption_.push(consumption_x10);
    sum_ += consumption_x10;
    const auto n = static_cast<std::int32_t>(consumption_.size());
    const std::int32_t average = sum_ / n;
    if (average <= 0) {
        return kRangeUnknown;
    }
    return static_cast<std::int16_t>(detail::clamp<std::int32_t>(static_cast<std::int32_t>(fuel_dl) * 1000 / average, 0, 2000));
}

void RangeEstimator::reset()
{
    consumption_.clear();
    sum_ = 0;
}

// ------------------------------------------------------------------------------------------ CAN message follow
namespace {
using FollowSigFollowing = detail::Signal<0, 1, 1, 0, 0, 1>;
using FollowSigWarning = detail::Signal<1, 1, 1, 0, 0, 1>;
using FollowSigAccel = detail::Signal<2, 10, 1, -400, -400, 200>;
using FollowSigGap = detail::Signal<12, 8, 1, 0, 0, 250>;
}  // namespace

bool pack_follow(const FollowMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return FollowSigFollowing::encode(frame, m.following) &&
           FollowSigWarning::encode(frame, m.warning) &&
           FollowSigAccel::encode(frame, m.accel) &&
           FollowSigGap::encode(frame, m.gap);
}

FollowMsg unpack_follow(const std::array<std::uint8_t, 8>& frame)
{
    FollowMsg m{};
    m.following = FollowSigFollowing::decode(frame);
    m.warning = FollowSigWarning::decode(frame);
    m.accel = FollowSigAccel::decode(frame);
    m.gap = FollowSigGap::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message immo
namespace {
using ImmoSigReleased = detail::Signal<0, 1, 1, 0, 0, 1>;
using ImmoSigBlocked = detail::Signal<1, 1, 1, 0, 0, 1>;
using ImmoSigChallenge = detail::Signal<8, 32, 1, 0, 0, 2000000000>;
}  // namespace

bool pack_immo(const ImmoMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return ImmoSigReleased::encode(frame, m.released) &&
           ImmoSigBlocked::encode(frame, m.blocked) &&
           ImmoSigChallenge::encode(frame, m.challenge);
}

ImmoMsg unpack_immo(const std::array<std::uint8_t, 8>& frame)
{
    ImmoMsg m{};
    m.released = ImmoSigReleased::decode(frame);
    m.blocked = ImmoSigBlocked::decode(frame);
    m.challenge = ImmoSigChallenge::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message range
namespace {
using RangeSigRangeKm = detail::Signal<0, 11, 1, -1, -1, 2000>;
using RangeSigFuelDl = detail::Signal<11, 10, 1, 0, 0, 1000>;
using RangeSigAvgX10 = detail::Signal<21, 10, 1, 0, 0, 1000>;
}  // namespace

bool pack_range(const RangeMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return RangeSigRangeKm::encode(frame, m.rangeKm) &&
           RangeSigFuelDl::encode(frame, m.fuelDl) &&
           RangeSigAvgX10::encode(frame, m.avgX10);
}

RangeMsg unpack_range(const std::array<std::uint8_t, 8>& frame)
{
    RangeMsg m{};
    m.rangeKm = RangeSigRangeKm::decode(frame);
    m.fuelDl = RangeSigFuelDl::decode(frame);
    m.avgX10 = RangeSigAvgX10::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message cylinders
namespace {
using CylindersSigActive = detail::Signal<0, 4, 1, 0, 0, 15>;
using CylindersSigTransitions = detail::Signal<4, 16, 1, 0, 0, 65535>;
using CylindersSigKnock = detail::Signal<20, 4, 1, 0, 0, 15>;
}  // namespace

bool pack_cylinders(const CylindersMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return CylindersSigActive::encode(frame, m.active) &&
           CylindersSigTransitions::encode(frame, m.transitions) &&
           CylindersSigKnock::encode(frame, m.knock);
}

CylindersMsg unpack_cylinders(const std::array<std::uint8_t, 8>& frame)
{
    CylindersMsg m{};
    m.active = CylindersSigActive::decode(frame);
    m.transitions = CylindersSigTransitions::decode(frame);
    m.knock = CylindersSigKnock::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message air
namespace {
using AirSigMapHpa = detail::Signal<0, 12, 1, 0, 0, 4000>;
using AirSigMafMg = detail::Signal<12, 11, 1, 0, 0, 2000>;
using AirSigPlausible = detail::Signal<23, 1, 1, 0, 0, 1>;
using AirSigIntakeC = detail::Signal<24, 8, 1, -40, -40, 215>;
}  // namespace

bool pack_air(const AirMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return AirSigMapHpa::encode(frame, m.mapHpa) &&
           AirSigMafMg::encode(frame, m.mafMg) &&
           AirSigPlausible::encode(frame, m.plausible) &&
           AirSigIntakeC::encode(frame, m.intakeC);
}

AirMsg unpack_air(const std::array<std::uint8_t, 8>& frame)
{
    AirMsg m{};
    m.mapHpa = AirSigMapHpa::decode(frame);
    m.mafMg = AirSigMafMg::decode(frame);
    m.plausible = AirSigPlausible::decode(frame);
    m.intakeC = AirSigIntakeC::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message starter
namespace {
using StarterSigStState = detail::Signal<0, 2, 1, 0, 0, 2>;
using StarterSigStarts = detail::Signal<2, 16, 1, 0, 0, 65535>;
using StarterSigStalls = detail::Signal<18, 16, 1, 0, 0, 65535>;
}  // namespace

bool pack_starter(const StarterMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return StarterSigStState::encode(frame, m.stState) &&
           StarterSigStarts::encode(frame, m.starts) &&
           StarterSigStalls::encode(frame, m.stalls);
}

StarterMsg unpack_starter(const std::array<std::uint8_t, 8>& frame)
{
    StarterMsg m{};
    m.stState = StarterSigStState::decode(frame);
    m.starts = StarterSigStarts::decode(frame);
    m.stalls = StarterSigStalls::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message battery
namespace {
using BatterySigSocPermille = detail::Signal<0, 10, 1, 0, 0, 1000>;
using BatterySigCellMaxC = detail::Signal<10, 8, 1, -40, -40, 100>;
using BatterySigCellMinC = detail::Signal<18, 8, 1, -40, -40, 100>;
using BatterySigLimitPct = detail::Signal<26, 7, 1, 0, 0, 100>;
}  // namespace

bool pack_battery(const BatteryMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return BatterySigSocPermille::encode(frame, m.socPermille) &&
           BatterySigCellMaxC::encode(frame, m.cellMaxC) &&
           BatterySigCellMinC::encode(frame, m.cellMinC) &&
           BatterySigLimitPct::encode(frame, m.limitPct);
}

BatteryMsg unpack_battery(const std::array<std::uint8_t, 8>& frame)
{
    BatteryMsg m{};
    m.socPermille = BatterySigSocPermille::decode(frame);
    m.cellMaxC = BatterySigCellMaxC::decode(frame);
    m.cellMinC = BatterySigCellMinC::decode(frame);
    m.limitPct = BatterySigLimitPct::decode(frame);
    return m;
}

// ------------------------------------------------------------------------------------------ CAN message clutch
namespace {
using ClutchSigClosed = detail::Signal<0, 1, 1, 0, 0, 1>;
using ClutchSigHot = detail::Signal<1, 1, 1, 0, 0, 1>;
using ClutchSigPressure = detail::Signal<2, 10, 1, 0, 0, 1000>;
using ClutchSigSlip = detail::Signal<12, 12, 1, -2000, -2000, 2000>;
}  // namespace

bool pack_clutch(const ClutchMsg& m, std::array<std::uint8_t, 8>& frame)
{
    frame.fill(0);
    return ClutchSigClosed::encode(frame, m.closed) &&
           ClutchSigHot::encode(frame, m.hot) &&
           ClutchSigPressure::encode(frame, m.pressure) &&
           ClutchSigSlip::encode(frame, m.slip);
}

ClutchMsg unpack_clutch(const std::array<std::uint8_t, 8>& frame)
{
    ClutchMsg m{};
    m.closed = ClutchSigClosed::decode(frame);
    m.hot = ClutchSigHot::decode(frame);
    m.pressure = ClutchSigPressure::decode(frame);
    m.slip = ClutchSigSlip::decode(frame);
    return m;
}
}  // namespace pt
