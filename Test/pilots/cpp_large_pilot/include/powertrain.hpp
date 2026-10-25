// powertrain.hpp - interface of the powertrain controller (powertrain.cpp).
#ifndef POWERTRAIN_HPP
#define POWERTRAIN_HPP

#include "pt_templates.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace pt {

std::int16_t idle_target_rpm(std::int16_t coolant_c);
std::int16_t cold_enrichment(std::int16_t coolant_c);
std::int16_t temperature_derate(std::int16_t oil_c);
std::int16_t pedal_torque(std::int16_t pedal_pct, std::int16_t rpm);
std::int16_t base_advance(std::int16_t rpm, std::int16_t load_pct);
std::int32_t q8_multiply(std::int32_t a_raw, std::int32_t b_raw);
std::int32_t q12_divide(std::int32_t a_raw, std::int32_t b_raw);
std::int32_t q8_to_int(std::int32_t raw);
std::int16_t saturate_i16(std::int32_t wide);
std::uint16_t saturate_u16(std::int32_t wide);
bool all_ready(bool a, bool b, bool c, bool d);
bool any_request(bool a, bool b, bool c);

enum class SyncState : std::uint8_t { Lost, Searching, Synced };
constexpr std::uint32_t kStallPeriodUs = 50000u;

class EngineSpeed {
public:
    explicit EngineSpeed(std::uint8_t teeth = 60, std::uint8_t missing = 2);
    SyncState tooth(std::uint32_t period_us);
    std::int16_t rpm() const;
    std::int16_t filtered_rpm() const;
    bool overspeed(std::int16_t limit) const;
    std::uint16_t sync_errors() const;
    void reset();

private:
    std::uint8_t teeth_;
    std::uint8_t missing_;
    SyncState sync_ = SyncState::Lost;
    std::uint16_t count_ = 0;
    std::int16_t rpm_ = 0;
    std::int16_t filtered_ = 0;
    std::uint32_t last_period_ = 0;
    std::uint16_t sync_errors_ = 0;
    detail::Ema<std::int16_t, 2> rpm_filter_;
};

enum class MonitorState : std::uint8_t { Ok, Pending, Failed, Latched };
constexpr std::int16_t kPedalMinRaw = 400;
constexpr std::int16_t kPedalMaxRaw = 3600;
constexpr std::int16_t kPedalTolerance = 120;
constexpr std::int16_t kBrakeOverridePct = 10;

struct PedalResult {
    std::int16_t percent;
    MonitorState monitor;
    bool limp_home;
    bool brake_override;
};
PedalResult pedal_position(std::int16_t track1_raw, std::int16_t track2_raw, bool brake, std::uint16_t dt_ms);
void pedal_reset();

enum class TorqueSource : std::uint8_t { Driver, Cruise, SpeedLimiter, Limit, Stability, Shift, Minimum };
struct TorqueRequests {
    std::int16_t driver;
    std::int16_t cruise;
    bool cruise_active;
    std::int16_t speed_limit;
    bool speed_limit_active;
    std::int16_t gear;
    std::int16_t oil_temp;
    std::int16_t stability;
    bool stability_reduce;
    bool shift_intervention;
    std::int16_t minimum;
};

class TorqueArbiter {
public:
    explicit TorqueArbiter(std::int16_t max_torque);
    std::int16_t arbitrate(const TorqueRequests& req);
    TorqueSource source() const;
    void reset();

private:
    std::int16_t max_torque_;
    TorqueSource source_ = TorqueSource::Driver;
    detail::RateLimiter<std::int16_t> shaper_;
};

constexpr std::int16_t kIdlePedalPct = 2;
constexpr std::int16_t kIdleMaxKph = 5;
constexpr std::int32_t kIdleLowBatteryMv = 12000;
constexpr std::int16_t kStallRpm = 500;

struct IdleInputs {
    bool engine_running;
    std::int16_t pedal_pct;
    std::int16_t vehicle_kph;
    std::int16_t coolant_c;
    bool ac_on;
    bool steering_load;
    std::int32_t battery_mv;
    std::int16_t rpm;
};

class IdleController {
public:
    IdleController();
    std::int16_t step(const IdleInputs& in);
    std::int16_t target() const;
    bool active() const;
    void reset();

private:
    detail::Pid<std::int16_t> pid_;
    bool active_ = false;
    std::int16_t target_ = 0;
};

constexpr std::int16_t kIntakeHotC = 60;
constexpr std::int16_t kBoostMaxHpa = 1100;
constexpr std::int16_t kOverboostHpa = 200;
constexpr std::uint16_t kOverboostMs = 500;

class BoostController {
public:
    BoostController();
    std::int16_t step(std::int16_t rpm, std::int16_t boost_hpa, std::int16_t intake_c, bool knock_active, std::uint16_t dt_ms);
    bool fault() const;
    void reset();

private:
    detail::Pid<std::int16_t> pid_;
    std::uint16_t overboost_ms_ = 0;
    bool fault_ = false;
};

enum class CutReason : std::uint8_t { None, Overspeed, Overrun, Crash };
constexpr std::int16_t kRevLimitRpm = 6500;
constexpr std::int16_t kOverrunCutRpm = 1400;
constexpr std::int16_t kOverrunMinCoolant = 40;
constexpr std::int32_t kInjectorUsPerMg = 35;
constexpr std::int32_t kMaxPulseUs = 20000;

struct InjectionInputs {
    std::int16_t rpm;
    std::int16_t pedal_pct;
    std::int16_t coolant_c;
    bool crash;
    std::uint16_t airmass_mg;
    bool lambda_closed_loop;
    std::int32_t lambda_correction;
    std::int32_t battery_mv;
};
struct InjectionResult {
    std::uint16_t pulse_us;
    CutReason cut;
    bool saturated;
};
InjectionResult injection(const InjectionInputs& in);

constexpr std::uint8_t kCylinders = 4;
constexpr std::int16_t kKnockStep = 3;
constexpr std::int16_t kKnockMaxRetard = 15;

class KnockControl {
public:
    KnockControl();
    std::int16_t cylinder(std::uint8_t cyl, std::uint16_t intensity, std::uint16_t threshold, std::int16_t rpm);
    std::int16_t max_retard() const;
    std::uint32_t events(std::uint8_t cyl) const;
    bool active() const;
    void reset();

private:
    std::array<std::int16_t, kCylinders> retard_{};
    std::array<std::uint16_t, kCylinders> recover_{};
    std::array<std::uint32_t, kCylinders> events_{};
};

constexpr std::int32_t kMinAdvance = -100;
constexpr std::int32_t kMaxAdvance = 450;
std::int16_t ignition_advance(std::int16_t rpm, std::int16_t load_pct, std::int16_t coolant_c, std::int16_t knock_retard,
                              bool idle_active, std::int16_t idle_error_rpm);

enum class DriveMode : std::uint8_t { Eco, Sport, Manual };
class ShiftSchedule {
public:
    explicit ShiftSchedule(DriveMode mode = DriveMode::Eco);
    std::int16_t select(std::int16_t speed_kph, std::int16_t pedal_pct, bool kickdown, std::int16_t manual_request);
    void set_mode(DriveMode mode);
    std::int16_t gear() const;
    void reset();

private:
    DriveMode mode_;
    std::int16_t gear_ = 1;
};

enum class LockupState : std::uint8_t { Open, Slipping, Locked };
constexpr std::int16_t kLockupMinOilC = 30;
constexpr std::int16_t kLockupKph = 50;
constexpr std::int16_t kLockupSlipRpm = 80;
LockupState converter_lockup(std::int16_t gear, std::int16_t speed_kph, std::int16_t slip_rpm, std::int16_t pedal_pct,
                             std::int16_t oil_c, bool shifting);

enum class CruiseState : std::uint8_t { Off, Standby, Active, Override };
constexpr std::int16_t kCruiseMinKph = 30;
constexpr std::int16_t kCruiseMaxKph = 180;
constexpr std::int16_t kCruiseOverridePct = 20;
constexpr std::int16_t kCruiseStep = 2;

struct CruiseInputs {
    bool main_switch;
    bool set;
    bool resume;
    bool cancel;
    bool accel;
    bool decel;
    bool brake;
    bool clutch;
    bool stability_active;
    std::int16_t speed_kph;
    std::int16_t pedal_pct;
};

class CruiseControl {
public:
    CruiseState step(const CruiseInputs& in);
    std::int16_t torque_request(std::int16_t speed_kph, std::int16_t accel_cms2) const;
    std::int16_t set_speed() const;
    std::uint16_t cancels() const;
    void reset();

private:
    CruiseState state_ = CruiseState::Off;
    std::int16_t set_kph_ = 0;
    std::uint16_t cancels_ = 0;
};

constexpr std::int16_t kAfterRunC = 100;
constexpr std::uint16_t kAfterRunMs = 60000;

class CoolingControl {
public:
    std::uint8_t fan(std::int16_t coolant_c, bool ac_on, bool engine_running, std::uint16_t dt_ms);
    bool thermostat(std::int16_t coolant_c, std::int16_t load_pct) const;
    void reset();

private:
    std::uint16_t after_run_ms_ = 0;
    bool was_running_ = false;
};

constexpr std::uint16_t kPrimeMs = 2000;

class FuelPump {
public:
    FuelPump();
    std::uint8_t step(std::int16_t rail_bar_x10, std::int16_t demand_mg, bool engine_running, bool crash);
    void reset();

private:
    detail::Pid<std::int16_t> pid_;
    std::uint16_t prime_ms_ = 0;
};
MonitorState monitor_CoolantSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_CoolantSensorMonitor_reset();
MonitorState monitor_MafPlausibility(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_MafPlausibility_reset();
MonitorState monitor_ThrottleTracking(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_ThrottleTracking_reset();
MonitorState monitor_SpeedSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_SpeedSensorMonitor_reset();
MonitorState monitor_BrakeSwitchMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_BrakeSwitchMonitor_reset();
MonitorState monitor_OilPressureMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_OilPressureMonitor_reset();

constexpr std::uint16_t kMisfireWindow = 200;
constexpr std::int32_t kMisfireDrop = 400;
constexpr std::uint32_t kCatalystDamagePct = 10;
constexpr std::uint32_t kEmissionPct = 2;
struct MisfireResult {
    bool misfire_now;
    bool window_done;
    std::uint8_t worst_cylinder;
    bool catalyst_damage;
    bool emission_relevant;
};
class MisfireCounter {
public:
    MisfireResult revolution(const std::array<std::int16_t, kCylinders>& accel);
    void reset();

private:
    std::array<std::uint16_t, kCylinders> misfires_{};
    std::uint16_t revolutions_ = 0;
};

struct EngineStatus {
    std::int32_t rpm;
    std::int32_t coolant_c;
    std::int32_t torque_nm;
    std::int32_t gear;
    std::int32_t pedal_pct;
    std::int32_t cruise_kph;
    bool mil;
    std::int32_t alive;
};
bool pack_engine_status(const EngineStatus& s, std::array<std::uint8_t, 8>& frame);
EngineStatus unpack_engine_status(const std::array<std::uint8_t, 8>& frame);
std::uint8_t frame_crc(const std::array<std::uint8_t, 8>& frame);

constexpr std::uint8_t kAliveMaxDelta = 2;
constexpr std::uint8_t kAliveMaxErrors = 3;
class AliveCheck {
public:
    bool frame(std::uint8_t counter);
    std::uint8_t errors() const;
    void reset();

private:
    bool started_ = false;
    std::uint8_t last_ = 0;
    std::uint8_t errors_ = 0;
};

enum class EventCode : std::uint8_t { Start, Stop, Knock, Overboost, Misfire, Limp };
struct Event {
    EventCode code;
    std::uint32_t time_ms;
    std::int16_t value;
};
class EventLog {
public:
    bool record(EventCode code, std::uint32_t time_ms, std::int16_t value);
    std::size_t count(EventCode code) const;
    std::size_t count_above(std::int16_t value) const;
    bool oldest(Event& out);
    std::size_t size() const;
    std::size_t overflows() const;
    void clear();

private:
    detail::RingBuffer<Event, 16> events_;
};

constexpr std::int16_t kThrottleMinPct = 2;
class ThrottleBody {
public:
    ThrottleBody();
    std::int16_t step(std::int16_t target_pct, std::int16_t position_pct, bool monitor_failed);
    bool limp_home() const;
    void reset();

private:
    detail::Pid<std::int16_t> pid_;
    bool limp_ = false;
};

constexpr std::uint16_t kMaxStopMs = 60000;
constexpr std::uint16_t kStopDelayMs = 1000;
struct StopStartInputs {
    bool enabled;
    bool battery_ok;
    std::int16_t coolant_c;
    bool ac_max;
    bool driver_belt;
    bool bonnet_closed;
    bool engine_running;
    bool brake;
    bool steering;
    std::int16_t speed_kph;
};
class StopStart {
public:
    enum class Decision : std::uint8_t { Hold, Stop, Restart };
    Decision step(const StopStartInputs& in, std::uint16_t dt_ms);
    void reset();

private:
    std::uint16_t stopped_ms_ = 0;
    std::uint16_t standstill_ms_ = 0;
    bool moved_ = false;
};

constexpr std::uint32_t kHeaterDelayMs = 20000;
std::uint8_t lambda_heater(std::int16_t sensor_c, std::int16_t exhaust_c, std::uint32_t since_start_ms);

class ISensorBus {
public:
    virtual ~ISensorBus() = default;
    virtual bool read(std::uint8_t channel, std::uint16_t& raw) = 0;
};
constexpr std::uint8_t kSensorChannels = 6;
constexpr std::uint8_t kSensorStaleLimit = 3;
class SensorHub {
public:
    explicit SensorHub(ISensorBus& bus);
    bool refresh();
    std::uint16_t value(std::uint8_t channel) const;
    bool stale(std::uint8_t channel) const;

private:
    ISensorBus& bus_;
    std::array<std::uint16_t, kSensorChannels> values_{};
    std::array<std::uint8_t, kSensorChannels> stale_{};
};
std::int16_t cam_phaser_b1_intake(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10, std::int16_t oil_c);
bool cam_phaser_b1_intake_locked();
std::int16_t cam_phaser_b1_intake_target();
bool cam_phaser_b1_intake_fault();
void cam_phaser_b1_intake_reset();
std::int16_t cam_phaser_b1_exhaust(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10, std::int16_t oil_c);
bool cam_phaser_b1_exhaust_locked();
std::int16_t cam_phaser_b1_exhaust_target();
bool cam_phaser_b1_exhaust_fault();
void cam_phaser_b1_exhaust_reset();
std::int16_t cam_phaser_b2_intake(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10, std::int16_t oil_c);
bool cam_phaser_b2_intake_locked();
std::int16_t cam_phaser_b2_intake_target();
bool cam_phaser_b2_intake_fault();
void cam_phaser_b2_intake_reset();
std::int16_t cam_phaser_b2_exhaust(std::int16_t rpm, std::int16_t load_pct, std::int16_t actual_deg, std::int16_t oil_bar_x10, std::int16_t oil_c);
bool cam_phaser_b2_exhaust_locked();
std::int16_t cam_phaser_b2_exhaust_target();
bool cam_phaser_b2_exhaust_fault();
void cam_phaser_b2_exhaust_reset();
enum class InjectorFault : std::uint8_t { None, Open, Short, Imbalance };
constexpr std::uint16_t kInjectorOpenMa = 300;
constexpr std::uint16_t kInjectorShortMa = 9000;
constexpr std::uint16_t kInjectorLeakMa = 100;
constexpr std::uint8_t kInjectorFaultCount = 5;
constexpr std::int16_t kInjectorBalanceLimit = 400;
InjectorFault injector_1_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta);
std::int16_t injector_1_balance();
void injector_1_reset();
InjectorFault injector_2_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta);
std::int16_t injector_2_balance();
void injector_2_reset();
InjectorFault injector_3_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta);
std::int16_t injector_3_balance();
void injector_3_reset();
InjectorFault injector_4_check(std::uint16_t current_ma, bool commanded, std::int16_t crank_accel_delta);
std::int16_t injector_4_balance();
void injector_4_reset();
std::int16_t wheel_slip_fl(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms);
bool wheel_slip_fl_active();
void wheel_slip_fl_reset();
std::int16_t wheel_slip_fr(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms);
bool wheel_slip_fr_active();
void wheel_slip_fr_reset();
std::int16_t wheel_slip_rl(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms);
bool wheel_slip_rl_active();
void wheel_slip_rl_reset();
std::int16_t wheel_slip_rr(std::int16_t wheel_kph_x10, std::int16_t vehicle_kph_x10, bool driven, std::uint16_t dt_ms);
bool wheel_slip_rr_active();
void wheel_slip_rr_reset();
constexpr std::int16_t kSlipLimit = 120;
constexpr std::uint16_t kSlipConfirmMs = 50;
std::int16_t traction_torque_limit(std::int16_t requested, std::int16_t slip_max, bool tcs_off);

constexpr std::int16_t kEgrMinCoolant = 60;
constexpr std::int16_t kEgrMaxLoad = 80;
constexpr std::int16_t kEgrMaxRpm = 4000;
class EgrValve {
public:
    EgrValve();
    std::uint8_t step(std::int16_t rpm, std::int16_t load_pct, std::int16_t coolant_c, std::int16_t measured_pct);
    std::int16_t target() const;
    void reset();

private:
    detail::Pid<std::int16_t> pid_;
    std::int16_t target_ = 0;
};

enum class DpfState : std::uint8_t { Loading, RegenRequested, Regenerating, Blocked };
constexpr std::int16_t kDpfBurnC = 550;
constexpr std::int32_t kDpfMaxSootMg = 40000;
constexpr std::int32_t kDpfRegenSootMg = 28000;
constexpr std::int32_t kDpfCleanSootMg = 4000;
constexpr std::int16_t kDpfRegenMinKph = 60;
constexpr std::int16_t kDpfBlockedHpa = 400;
constexpr std::uint32_t kDpfRegenMaxMs = 1200000;
class ParticulateFilter {
public:
    DpfState step(std::int16_t soot_mg_per_s, std::int16_t exhaust_c, std::int16_t speed_kph, std::int16_t diff_pressure_hpa,
                  std::uint16_t dt_ms);
    std::int32_t soot() const;
    std::uint16_t regenerations() const;
    void set_soot(std::int32_t mg);
    void reset();

private:
    DpfState state_ = DpfState::Loading;
    std::int32_t soot_mg_ = 0;
    std::uint32_t regen_ms_ = 0;
    std::uint16_t regenerations_ = 0;
};

constexpr std::int16_t kScrMinC = 200;
constexpr std::int16_t kScrMaxC = 450;
constexpr std::int32_t kScrMaxDose = 5000;
std::uint16_t scr_dosing(std::int16_t nox_ppm_in, std::int16_t exhaust_flow_kgh, std::int16_t catalyst_c, std::int16_t tank_pct,
                         bool frozen);
std::uint8_t evap_purge(std::int16_t coolant_c, bool closed_loop, std::int16_t load_pct, std::int16_t trim_pct,
                        std::uint32_t since_start_ms);
constexpr std::int16_t kTrimLimit = 250;
constexpr std::int16_t kKnockAdaptLimit = 60;
bool fuel_trim_learn(std::int16_t rpm, std::int16_t load_pct, std::int16_t short_term_pct_x10, bool closed_loop, std::int16_t coolant_c);
std::int16_t fuel_trim(std::int16_t rpm, std::int16_t load_pct);
bool knock_adapt_learn(std::int16_t rpm, std::int16_t load_pct, std::int16_t retard_x10);
std::int16_t knock_adapt(std::int16_t rpm, std::int16_t load_pct);
std::uint32_t adaptation_updates();
void adaptation_reset();

constexpr std::uint32_t kOcvRestMs = 1800000;
class BatteryState {
public:
    explicit BatteryState(std::int32_t capacity_mas);
    std::int16_t update(std::int32_t current_ma, std::int16_t voltage_mv_cell, bool resting, std::uint16_t dt_ms);
    std::int16_t soc() const;
    void set_soc(std::int16_t permille);
    static std::int16_t ocv_to_soc(std::int16_t mv);
    void reset();

private:
    std::int32_t capacity_mas_;
    std::int64_t charge_mas_ = 0;
    std::uint32_t rest_ms_ = 0;
};

constexpr std::int16_t kRegenMinKph = 8;
constexpr std::int16_t kRegenTaperSoc = 800;
constexpr std::int16_t kRegenFullSoc = 950;
struct RegenSplit {
    std::int16_t regen_nm;
    std::int16_t friction_nm;
};
RegenSplit regen_split(std::int16_t brake_request_nm, std::int16_t soc_permille, std::int16_t speed_kph, std::int16_t motor_max_nm,
                       bool abs_active, std::int16_t battery_c);
std::int16_t alternator_target_mv(std::int16_t battery_c, bool engine_running, bool decelerating, bool high_load, std::int16_t soc_pct);

enum class StarterState : std::uint8_t { Idle, Cranking, Protect };
constexpr std::int16_t kStarterReleaseRpm = 600;
constexpr std::uint16_t kStarterMaxMs = 10000;
constexpr std::uint16_t kStarterCoolMs = 30000;
class Starter {
public:
    StarterState step(bool request, bool clutch_or_park, bool immo_released, std::int16_t rpm, std::uint16_t dt_ms);
    void reset();

private:
    StarterState state_ = StarterState::Idle;
    std::uint16_t crank_ms_ = 0;
    std::uint16_t cool_ms_ = 0;
};

std::int16_t launch_rpm_limit(bool armed, std::int16_t speed_kph, std::int16_t pedal_pct, std::int16_t clutch_pct, bool tcs_on);

constexpr std::int16_t kHillMinSlope = 3;
constexpr std::uint16_t kHillHoldMs = 2000;
constexpr std::int16_t kHillTorquePerPct = 12;
class HillHold {
public:
    bool step(std::int16_t slope_pct, bool brake, std::int16_t speed_kph, std::int16_t engine_torque, std::uint16_t dt_ms);
    void reset();

private:
    bool holding_ = false;
    std::uint16_t hold_ms_ = 0;
};

std::int16_t speed_limiter_torque(bool active, std::int16_t limit_kph, std::int16_t speed_kph, std::int16_t pedal_pct,
                                  std::int16_t driver_nm);

enum class ServiceDue : std::uint8_t { No, Soon, Now };
constexpr std::uint32_t kServiceKm = 30000;
constexpr std::uint32_t kServiceDays = 730;
ServiceDue service_due(std::uint32_t km_since, std::uint32_t days_since, std::uint32_t oil_quality_pct);
std::int16_t oil_level_mm(std::int16_t raw_mm, std::int16_t oil_c, bool engine_off_minutes, std::int16_t slope_pct);

struct DriveProfile {
    std::int16_t pedal_gain_pct;
    DriveMode shift_mode;
    bool stop_start;
};
DriveProfile drive_profile(DriveMode mode, bool trailer, std::int16_t outside_c);

constexpr std::int16_t kClutchHotC = 250;
class ClutchControl {
public:
    ClutchControl();
    std::int16_t launch(std::int16_t engine_rpm, std::int16_t input_rpm, std::int16_t pedal_pct, std::int16_t clutch_c);
    bool closed() const;
    bool overheated() const;
    void reset();

private:
    detail::Pid<std::int16_t> pid_;
    bool closed_ = false;
    bool overheated_ = false;
};

constexpr std::uint16_t kGearTimeoutMs = 600;
class GearActuator {
public:
    struct Result {
        bool engaged;
        bool fault;
        std::int16_t force;
    };
    Result engage(std::int16_t target_gear, std::int16_t position_mm, std::int16_t speed_rpm_out, std::uint16_t dt_ms);
    void reset();

private:
    std::int16_t current_target_ = 0;
    std::uint16_t time_ms_ = 0;
    bool synchronizing_ = false;
};

bool idle_param_set(std::uint8_t index, std::int16_t value);
std::int16_t idle_param(std::uint8_t index);
bool limit_param_set(std::uint8_t index, std::int16_t value);
std::int16_t limit_param(std::uint8_t index);
bool timing_param_set(std::uint8_t index, std::uint16_t value);
std::uint16_t timing_param(std::uint8_t index);
struct ParamCheck {
    bool idle_ok;
    bool limit_ok;
    bool timing_ok;
};
ParamCheck params_check(std::uint16_t idle_crc, std::uint16_t limit_crc, std::uint16_t timing_crc);
std::uint16_t params_crc(std::uint8_t set);
void params_restore();

enum class ReadinessMonitor : std::uint8_t { Catalyst, Misfire, Fuel, Oxygen, Heater, Egr, Evap, Dpf };
class Readiness {
public:
    std::uint8_t complete(ReadinessMonitor m, bool passed);
    bool ready(std::uint8_t required_mask) const;
    bool mil() const;
    void new_cycle();
    void clear();

private:
    std::uint8_t complete_ = 0;
    std::uint8_t failed_ = 0;
};
struct TransmissionMsg {
    std::int32_t gear;
    std::int32_t targetGear;
    std::int32_t oilTemp;
    std::int32_t outputRpm;
    std::int32_t shifting;
};
bool pack_transmission(const TransmissionMsg& m, std::array<std::uint8_t, 8>& frame);
TransmissionMsg unpack_transmission(const std::array<std::uint8_t, 8>& frame);
struct BrakeMsg {
    std::int32_t pressure;
    std::int32_t abs;
    std::int32_t esp;
    std::int32_t decel;
    std::int32_t pedalSwitch;
};
bool pack_brake(const BrakeMsg& m, std::array<std::uint8_t, 8>& frame);
BrakeMsg unpack_brake(const std::array<std::uint8_t, 8>& frame);
struct WheelSpeedsMsg {
    std::int32_t fL;
    std::int32_t fR;
    std::int32_t rL;
    std::int32_t rR;
};
bool pack_wheel_speeds(const WheelSpeedsMsg& m, std::array<std::uint8_t, 8>& frame);
WheelSpeedsMsg unpack_wheel_speeds(const std::array<std::uint8_t, 8>& frame);
struct HybridMsg {
    std::int32_t soc;
    std::int32_t motorNm;
    std::int32_t batteryC;
    std::int32_t regen;
};
bool pack_hybrid(const HybridMsg& m, std::array<std::uint8_t, 8>& frame);
HybridMsg unpack_hybrid(const std::array<std::uint8_t, 8>& frame);
struct EmissionsMsg {
    std::int32_t soot;
    std::int32_t dpfState;
    std::int32_t scrDose;
    std::int32_t egr;
};
bool pack_emissions(const EmissionsMsg& m, std::array<std::uint8_t, 8>& frame);
EmissionsMsg unpack_emissions(const std::array<std::uint8_t, 8>& frame);
struct CruiseMsg {
    std::int32_t state;
    std::int32_t setKph;
    std::int32_t limit;
    std::int32_t limiterOn;
};
bool pack_cruise(const CruiseMsg& m, std::array<std::uint8_t, 8>& frame);
CruiseMsg unpack_cruise(const std::array<std::uint8_t, 8>& frame);
constexpr std::uint16_t kCoilMinPeakMa = 4000;
constexpr std::uint8_t kCoilFaultCount = 8;
std::uint16_t coil_1_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma);
bool coil_1_fault();
void coil_1_reset();
std::uint16_t coil_2_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma);
bool coil_2_fault();
void coil_2_reset();
std::uint16_t coil_3_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma);
bool coil_3_fault();
void coil_3_reset();
std::uint16_t coil_4_dwell(std::int32_t battery_mv, std::int16_t rpm, std::uint16_t primary_peak_ma);
bool coil_4_fault();
void coil_4_reset();
MonitorState monitor_IntakeTempMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_IntakeTempMonitor_reset();
MonitorState monitor_RailPressureMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_RailPressureMonitor_reset();
MonitorState monitor_CamCrankMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_CamCrankMonitor_reset();
MonitorState monitor_BatteryVoltageMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_BatteryVoltageMonitor_reset();
MonitorState monitor_CatalystTempMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_CatalystTempMonitor_reset();
MonitorState monitor_ClutchSwitchMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_ClutchSwitchMonitor_reset();
MonitorState monitor_BoostSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_BoostSensorMonitor_reset();
MonitorState monitor_GearPositionMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_GearPositionMonitor_reset();

constexpr std::int16_t kWarmUpC = 70;
constexpr std::int16_t kCoolantHotC = 112;
constexpr std::int16_t kGrilleOpenC = 95;
constexpr std::int16_t kOilHotC = 135;
constexpr std::int16_t kTurboHotC = 300;
struct ThermalInputs {
    std::int16_t coolant_c;
    std::int16_t oil_c;
    std::int16_t turbo_c;
    std::int16_t load_pct;
    std::int16_t speed_kph;
    bool heater_request;
    bool ac_on;
    bool engine_running;
};
struct ThermalOutputs {
    std::int16_t pump_pct;
    bool split_cooling;
    bool grille_open;
    bool derate;
    bool after_pump;
};
ThermalOutputs thermal_management(const ThermalInputs& in);

constexpr std::int32_t kCylinderCc = 500;
constexpr std::int32_t kAirPlausiblePct = 25;
struct AirMass {
    std::int16_t speed_density_mg;
    std::int16_t maf_mg;
    std::int16_t used_mg;
    bool plausible;
};
AirMass air_mass(std::int16_t map_hpa, std::int16_t intake_c, std::int16_t rpm, std::int16_t ve_pct, std::int16_t maf_mg);
std::int16_t indicated_torque(std::int16_t air_mg, std::int16_t advance_x10, std::int16_t optimum_x10);
std::int16_t friction_torque(std::int16_t rpm, std::int16_t oil_c, bool ac_on, bool alternator_high);
std::int16_t brake_torque(std::int16_t indicated, std::int16_t friction);

enum class ShiftPhase : std::uint8_t { Idle, Preselect, TorqueHandover, InertiaPhase, Done, Abort };
constexpr std::uint16_t kPreselectMs = 300;
constexpr std::uint16_t kHandoverMs = 400;
constexpr std::uint16_t kInertiaMs = 150;
class ShiftSequencer {
public:
    ShiftPhase step(bool request, std::int16_t torque_nm, std::int16_t clutch_off_pct, std::int16_t clutch_on_pct,
                    bool gear_engaged, std::uint16_t dt_ms);
    std::uint16_t shifts() const;
    std::uint16_t aborts() const;
    void reset();

private:
    ShiftPhase phase_ = ShiftPhase::Idle;
    std::uint16_t phase_ms_ = 0;
    std::uint16_t shifts_ = 0;
    std::uint16_t aborts_ = 0;
};

enum class EvMode : std::uint8_t { Hybrid, Electric, Charge };
constexpr std::int16_t kEvMinSoc = 300;
constexpr std::int16_t kEvMaxKph = 60;
constexpr std::int16_t kEvMaxDemandNm = 80;
constexpr std::int16_t kChargeSoc = 150;
constexpr std::uint16_t kEvMinDwellMs = 3000;
class EvManager {
public:
    EvMode step(std::int16_t soc_permille, std::int16_t demand_nm, std::int16_t speed_kph, bool ev_button, bool cold,
                std::uint16_t dt_ms);
    void reset();

private:
    EvMode mode_ = EvMode::Hybrid;
    std::uint16_t dwell_ms_ = 0;
};

constexpr std::uint16_t kCdaStableMs = 2000;
class CylinderDeactivation {
public:
    std::uint8_t step(std::int16_t rpm, std::int16_t load_pct, std::int16_t gear, std::int16_t coolant_c, bool knock,
                      std::uint16_t dt_ms);
    std::uint16_t transitions() const;
    void reset();

private:
    std::uint8_t active_mask_ = 0;
    std::uint16_t stable_ms_ = 0;
    std::uint16_t transitions_ = 0;
};

std::int16_t turbo_protection(std::int16_t turbo_rpm_k, std::int16_t exhaust_c, std::int16_t altitude_m, std::int16_t torque_nm);
std::int16_t vehicle_speed(const std::array<std::int16_t, 4>& wheels_x10, bool front_driven);
bool gear_ratio_plausible(std::int16_t gear, std::int16_t engine_rpm, std::int16_t output_rpm);
std::uint8_t exhaust_flap(DriveMode mode, std::int16_t rpm, bool night_quiet);
std::uint8_t coolant_pump_pwm(std::int16_t demand_pct, std::int32_t battery_mv);

bool store_freeze_frame(std::uint16_t dtc, std::int16_t rpm, std::int16_t coolant_c, std::int16_t speed_kph);
std::size_t freeze_frames();
enum class DiagSession : std::uint8_t { Default = 1, Programming = 2, Extended = 3 };
struct DiagRequest {
    std::uint8_t service;
    std::uint8_t sub;
    std::uint16_t data;
    std::int16_t speed_kph;
};
struct DiagResponse {
    bool positive;
    std::uint8_t nrc;
    std::uint16_t value;
};
DiagResponse diag_request(const DiagRequest& req);
DiagSession diag_session();
void diag_reset();
std::uint16_t diag_key_for(std::uint16_t seed);
constexpr std::int32_t kBrakeFadeC = 600;
std::int16_t brake_temp_fl(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms);
bool brake_fade_fl();
void brake_temp_fl_reset(std::int16_t ambient_c);
std::int16_t brake_temp_fr(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms);
bool brake_fade_fr();
void brake_temp_fr_reset(std::int16_t ambient_c);
std::int16_t brake_temp_rl(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms);
bool brake_fade_rl();
void brake_temp_rl_reset(std::int16_t ambient_c);
std::int16_t brake_temp_rr(std::int16_t brake_nm, std::int16_t wheel_kph, std::int16_t ambient_c, std::uint16_t dt_ms);
bool brake_fade_rr();
void brake_temp_rr_reset(std::int16_t ambient_c);
struct KnockWindow {
    std::int16_t start_deg;
    std::int16_t length_deg;
    std::uint16_t threshold;
    bool knock;
    bool valid;
};
KnockWindow knock_window_1(std::int16_t rpm, std::uint16_t integral, bool injection_noise);
void knock_window_1_reset();
KnockWindow knock_window_2(std::int16_t rpm, std::uint16_t integral, bool injection_noise);
void knock_window_2_reset();
KnockWindow knock_window_3(std::int16_t rpm, std::uint16_t integral, bool injection_noise);
void knock_window_3_reset();
KnockWindow knock_window_4(std::int16_t rpm, std::uint16_t integral, bool injection_noise);
void knock_window_4_reset();
std::int16_t oil_pump_control(std::int16_t target_bar_x10, std::int16_t measured_bar_x10, bool engine_running);
std::int16_t coolant_valve_control(std::int16_t target_c, std::int16_t measured_c, bool engine_running);
std::int16_t grille_control(std::int16_t target_pct, std::int16_t measured_pct, bool enabled);
std::int16_t bypass_control(std::int16_t target, std::int16_t measured, bool idle);
void control_loops_reset();

enum class EngineRun : std::uint8_t { Off, Cranking, Running, Stalled, Afterrun };
constexpr std::int16_t kRunRpm = 500;
constexpr std::uint16_t kCrankTimeoutMs = 8000;
constexpr std::uint16_t kAfterrunMs = 5000;
class EngineRunState {
public:
    EngineRun step(bool key_on, bool start_request, std::int16_t rpm, bool fuel_cut_request, std::uint16_t dt_ms);
    std::uint16_t starts() const;
    std::uint16_t failed_starts() const;
    std::uint16_t stalls() const;
    void reset();

private:
    EngineRun state_ = EngineRun::Off;
    std::uint16_t time_ms_ = 0;
    std::uint16_t starts_ = 0;
    std::uint16_t failed_starts_ = 0;
    std::uint16_t stalls_ = 0;
};

enum class HvState : std::uint8_t { Off, Precharge, On, Discharge, Fault };
constexpr std::int32_t kIsolationFaultKohm = 100;
constexpr std::int32_t kIsolationWarnKohm = 500;
constexpr std::int32_t kPrechargePct = 95;
constexpr std::uint16_t kPrechargeTimeoutMs = 2000;
constexpr std::int32_t kSafeLinkMv = 60000;
constexpr std::uint16_t kDischargeTimeoutMs = 5000;
struct HvInputs {
    bool request_on;
    bool interlock_closed;
    bool crash;
    std::int32_t isolation_kohm;
    std::int32_t battery_mv;
    std::int32_t link_mv;
};
class HighVoltage {
public:
    HvState step(const HvInputs& in, std::uint16_t dt_ms);
    bool isolation_fault() const;
    void reset();

private:
    HvState state_ = HvState::Off;
    std::uint16_t time_ms_ = 0;
    bool isolation_fault_ = false;
};

enum class ChargeState : std::uint8_t { Unplugged, Connected, Charging, Paused, Complete };
constexpr std::int16_t kChargeTargetSoc = 900;
constexpr std::int16_t kChargeMaxC = 45;
constexpr std::int16_t kChargeMinC = -10;
constexpr std::int16_t kChargerMaxA = 32;
class Charger {
public:
    ChargeState step(bool plug_locked, bool proximity, std::int16_t pilot_pct, std::int16_t soc_permille, std::int16_t battery_c,
                     bool user_stop);
    std::int16_t current_limit() const;
    void reset();

private:
    ChargeState state_ = ChargeState::Unplugged;
    std::int16_t current_limit_a_ = 0;
};

std::int16_t dcdc_target_mv(HvState hv, std::int16_t lv_battery_c, std::int16_t lv_soc_pct, bool high_load);
struct BatteryThermal {
    bool cooling;
    bool chiller;
    bool heater;
    std::int16_t power_limit_pct;
};
BatteryThermal battery_thermal(std::int16_t cell_max_c, std::int16_t cell_min_c, bool charging, bool driving);
std::int16_t shift_recommendation(std::int16_t gear, std::int16_t rpm, std::int16_t pedal_pct, bool manual_mode);
bool trailer_detected(std::int16_t mass_estimate_kg, std::int16_t curb_kg, bool trailer_socket, std::int16_t lamp_current_ma);
struct ThermalMsg {
    std::int32_t coolant;
    std::int32_t oil;
    std::int32_t pump;
    std::int32_t grille;
    std::int32_t derate;
};
bool pack_thermal(const ThermalMsg& m, std::array<std::uint8_t, 8>& frame);
ThermalMsg unpack_thermal(const std::array<std::uint8_t, 8>& frame);
struct HvStatusMsg {
    std::int32_t state;
    std::int32_t linkV;
    std::int32_t isolation;
    std::int32_t interlock;
};
bool pack_hv_status(const HvStatusMsg& m, std::array<std::uint8_t, 8>& frame);
HvStatusMsg unpack_hv_status(const std::array<std::uint8_t, 8>& frame);
struct ChargingMsg {
    std::int32_t chgState;
    std::int32_t currentA;
    std::int32_t soc;
    std::int32_t plugLocked;
};
bool pack_charging(const ChargingMsg& m, std::array<std::uint8_t, 8>& frame);
ChargingMsg unpack_charging(const std::array<std::uint8_t, 8>& frame);
struct DiagnosisMsg {
    std::int32_t mil;
    std::int32_t dtcCount;
    std::int32_t ready;
    std::int32_t misfire;
};
bool pack_diagnosis(const DiagnosisMsg& m, std::array<std::uint8_t, 8>& frame);
DiagnosisMsg unpack_diagnosis(const std::array<std::uint8_t, 8>& frame);
struct EngineTempsMsg {
    std::int32_t exhaust;
    std::int32_t turbo;
    std::int32_t intake;
    std::int32_t catalyst;
};
bool pack_engine_temps(const EngineTempsMsg& m, std::array<std::uint8_t, 8>& frame);
EngineTempsMsg unpack_engine_temps(const std::array<std::uint8_t, 8>& frame);
struct DriverInfoMsg {
    std::int32_t range;
    std::int32_t consumption;
    std::int32_t service;
    std::int32_t shiftHint;
};
bool pack_driver_info(const DriverInfoMsg& m, std::array<std::uint8_t, 8>& frame);
DriverInfoMsg unpack_driver_info(const std::array<std::uint8_t, 8>& frame);
MonitorState monitor_FuelLevelMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_FuelLevelMonitor_reset();
MonitorState monitor_ExhaustTempMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_ExhaustTempMonitor_reset();
MonitorState monitor_TransOilMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_TransOilMonitor_reset();
MonitorState monitor_HvIsolationMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_HvIsolationMonitor_reset();
MonitorState monitor_CellVoltageMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_CellVoltageMonitor_reset();
MonitorState monitor_ChargerPilotMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_ChargerPilotMonitor_reset();
MonitorState monitor_LambdaSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_LambdaSensorMonitor_reset();
MonitorState monitor_KnockSensorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_KnockSensorMonitor_reset();
MonitorState monitor_WheelSpeedMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_WheelSpeedMonitor_reset();
MonitorState monitor_ThrottleSpringMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_ThrottleSpringMonitor_reset();
MonitorState monitor_AltitudeMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_AltitudeMonitor_reset();
MonitorState monitor_ContactorMonitor(std::int16_t value, std::int16_t reference, std::uint16_t dt_ms);
void monitor_ContactorMonitor_reset();

enum class GapSetting : std::uint8_t { Near, Middle, Far };
class FollowControl {
public:
    std::int16_t step(std::int16_t own_kph, std::int16_t set_kph, bool target_valid, std::int16_t gap_m, std::int16_t closing_kph,
                      GapSetting setting);
    bool following() const;
    bool warning() const;
    void reset();

private:
    bool following_ = false;
    bool warning_ = false;
};

constexpr std::uint8_t kImmoMaxFailures = 3;
class Immobilizer {
public:
    bool challenge(std::uint32_t random);
    bool answer(std::uint32_t response, std::uint32_t secret);
    bool released() const;
    bool blocked() const;
    void reset();

private:
    std::uint32_t challenge_ = 0;
    bool released_ = false;
    std::uint8_t failures_ = 0;
    bool blocked_ = false;
};

class DriveCycle {
public:
    void start(std::int16_t coolant_c);
    void update(std::int16_t coolant_c, std::int16_t speed_kph, std::uint32_t run_ms);
    std::uint16_t warm_ups() const;
    std::uint16_t cycles() const;

private:
    std::int16_t start_coolant_ = 0;
    bool warm_seen_ = false;
    bool cycle_done_ = false;
    std::uint16_t warm_ups_ = 0;
    std::uint16_t cycles_ = 0;
};

constexpr std::size_t kRangeSamples = 8;
constexpr std::int16_t kRangeUnknown = -1;
class RangeEstimator {
public:
    std::int16_t update(std::int16_t consumption_x10, std::int16_t fuel_dl);
    void reset();

private:
    detail::RingBuffer<std::int16_t, kRangeSamples> consumption_;
    std::int32_t sum_ = 0;
};
struct FollowMsg {
    std::int32_t following;
    std::int32_t warning;
    std::int32_t accel;
    std::int32_t gap;
};
bool pack_follow(const FollowMsg& m, std::array<std::uint8_t, 8>& frame);
FollowMsg unpack_follow(const std::array<std::uint8_t, 8>& frame);
struct ImmoMsg {
    std::int32_t released;
    std::int32_t blocked;
    std::int32_t challenge;
};
bool pack_immo(const ImmoMsg& m, std::array<std::uint8_t, 8>& frame);
ImmoMsg unpack_immo(const std::array<std::uint8_t, 8>& frame);
struct RangeMsg {
    std::int32_t rangeKm;
    std::int32_t fuelDl;
    std::int32_t avgX10;
};
bool pack_range(const RangeMsg& m, std::array<std::uint8_t, 8>& frame);
RangeMsg unpack_range(const std::array<std::uint8_t, 8>& frame);
struct CylindersMsg {
    std::int32_t active;
    std::int32_t transitions;
    std::int32_t knock;
};
bool pack_cylinders(const CylindersMsg& m, std::array<std::uint8_t, 8>& frame);
CylindersMsg unpack_cylinders(const std::array<std::uint8_t, 8>& frame);
struct AirMsg {
    std::int32_t mapHpa;
    std::int32_t mafMg;
    std::int32_t plausible;
    std::int32_t intakeC;
};
bool pack_air(const AirMsg& m, std::array<std::uint8_t, 8>& frame);
AirMsg unpack_air(const std::array<std::uint8_t, 8>& frame);
struct StarterMsg {
    std::int32_t stState;
    std::int32_t starts;
    std::int32_t stalls;
};
bool pack_starter(const StarterMsg& m, std::array<std::uint8_t, 8>& frame);
StarterMsg unpack_starter(const std::array<std::uint8_t, 8>& frame);
struct BatteryMsg {
    std::int32_t socPermille;
    std::int32_t cellMaxC;
    std::int32_t cellMinC;
    std::int32_t limitPct;
};
bool pack_battery(const BatteryMsg& m, std::array<std::uint8_t, 8>& frame);
BatteryMsg unpack_battery(const std::array<std::uint8_t, 8>& frame);
struct ClutchMsg {
    std::int32_t closed;
    std::int32_t hot;
    std::int32_t pressure;
    std::int32_t slip;
};
bool pack_clutch(const ClutchMsg& m, std::array<std::uint8_t, 8>& frame);
ClutchMsg unpack_clutch(const std::array<std::uint8_t, 8>& frame);

}  // namespace pt

#endif
