/* body_ctrl.c - body control module of a passenger car (ASIL B parts marked in docs/requirements.md).
   One translation unit with the module's state machines, signal conditioning, CAN layer, diagnostics and
   parameter handling. Generic code is written with macros (the C form of templates) and _Generic. */
#include "bcm.h"
#include "bcm_hal.h"

#include <string.h>

/* ------------------------------------------------------------------------------------------ fixed point */
#define Q15_ONE 32767
#define BCM_ABS(x) ((x) < 0 ? -(x) : (x))

static int16_t clamp_i16(int16_t x, int16_t lo, int16_t hi)
{
    if (x < lo) {
        return lo;
    }
    if (x > hi) {
        return hi;
    }
    return x;
}

static int32_t clamp_i32(int32_t x, int32_t lo, int32_t hi)
{
    if (x < lo) {
        return lo;
    }
    if (x > hi) {
        return hi;
    }
    return x;
}

static uint16_t clamp_u16(uint16_t x, uint16_t lo, uint16_t hi)
{
    if (x < lo) {
        return lo;
    }
    if (x > hi) {
        return hi;
    }
    return x;
}

#define bcm_clamp(x, lo, hi) _Generic((x), int16_t: clamp_i16, int32_t: clamp_i32, uint16_t: clamp_u16)(x, lo, hi)

int16_t bcm_q15_mul(int16_t a, int16_t b)
{
    int32_t product = (int32_t)a * (int32_t)b;
    product = (product + (1 << 14)) >> 15;
    return (int16_t)clamp_i32(product, -32768, 32767);
}

int16_t bcm_sat_add_i16(int16_t a, int16_t b)
{
    int32_t sum = (int32_t)a + (int32_t)b;
    return (int16_t)clamp_i32(sum, -32768, 32767);
}

uint16_t bcm_sat_sub_u16(uint16_t a, uint16_t b)
{
    if (b > a) {
        return 0u;
    }
    return (uint16_t)(a - b);
}

int32_t bcm_div_round(int32_t num, int32_t den)
{
    if (den == 0) {
        return num >= 0 ? INT32_MAX : INT32_MIN;
    }
    if ((num < 0) != (den < 0)) {
        return (num - den / 2) / den;
    }
    return (num + den / 2) / den;
}

int16_t bcm_clamp_signal(int16_t value, int16_t lo, int16_t hi)
{
    return bcm_clamp(value, lo, hi);
}

uint16_t bcm_clamp_counter(uint16_t value, uint16_t lo, uint16_t hi)
{
    return bcm_clamp(value, lo, hi);
}

int32_t bcm_clamp_wide(int32_t value, int32_t lo, int32_t hi)
{
    return bcm_clamp(value, lo, hi);
}

/* ------------------------------------------------------------------------------- exponential averages */
#define DEFINE_EMA(name, type, shift)                                                   \
    static type name##_acc;                                                             \
    static bool name##_primed;                                                          \
    type name##_update(type sample)                                                     \
    {                                                                                   \
        if (!name##_primed) {                                                           \
            name##_acc = sample;                                                        \
            name##_primed = true;                                                       \
            return sample;                                                              \
        }                                                                               \
        name##_acc = (type)(name##_acc + ((sample - name##_acc) / (1 << (shift))));     \
        return name##_acc;                                                              \
    }                                                                                   \
    void name##_reset(void)                                                             \
    {                                                                                   \
        name##_acc = 0;                                                                 \
        name##_primed = false;                                                          \
    }

DEFINE_EMA(ema_battery_mv, int32_t, 3)
DEFINE_EMA(ema_light_lux, int32_t, 2)
DEFINE_EMA(ema_cabin_temp, int16_t, 2)
DEFINE_EMA(ema_rain_level, int16_t, 1)
DEFINE_EMA(ema_vehicle_speed, int16_t, 2)

/* ----------------------------------------------------------------------------------------- lookup tables */
#define DEFINE_LUT(name, N)                                                             \
    int16_t name##_lookup(int16_t x)                                                    \
    {                                                                                   \
        int i;                                                                          \
        if (x <= name##_x[0]) {                                                         \
            return name##_y[0];                                                         \
        }                                                                               \
        for (i = 1; i < (N); i++) {                                                     \
            if (x <= name##_x[i]) {                                                     \
                int32_t dx = (int32_t)name##_x[i] - name##_x[i - 1];                    \
                int32_t dy = (int32_t)name##_y[i] - name##_y[i - 1];                    \
                return (int16_t)(name##_y[i - 1] + (dy * (x - name##_x[i - 1])) / dx);  \
            }                                                                           \
        }                                                                               \
        return name##_y[(N) - 1];                                                       \
    }

static const int16_t lut_wiper_interval_x[5] = {0, 30, 60, 100, 140};
static const int16_t lut_wiper_interval_y[5] = {4000, 3000, 2000, 1200, 800};
DEFINE_LUT(lut_wiper_interval, 5)

static const int16_t lut_headlamp_level_x[5] = {-20, -10, 0, 10, 20};
static const int16_t lut_headlamp_level_y[5] = {0, 25, 50, 75, 100};
DEFINE_LUT(lut_headlamp_level, 5)

static const int16_t lut_seat_heat_target_x[4] = {0, 1, 2, 3};
static const int16_t lut_seat_heat_target_y[4] = {0, 300, 380, 450};
DEFINE_LUT(lut_seat_heat_target, 4)

static const int16_t lut_fan_duty_x[6] = {0, 100, 200, 300, 400, 500};
static const int16_t lut_fan_duty_y[6] = {0, 10, 25, 45, 70, 100};
DEFINE_LUT(lut_fan_duty, 6)

static const int16_t lut_dim_curve_x[6] = {0, 20, 40, 60, 80, 100};
static const int16_t lut_dim_curve_y[6] = {0, 2, 8, 20, 45, 100};
DEFINE_LUT(lut_dim_curve, 6)

static const int16_t lut_bulb_current_x[5] = {9000, 11000, 12000, 13000, 14500};
static const int16_t lut_bulb_current_y[5] = {700, 860, 940, 1010, 1100};
DEFINE_LUT(lut_bulb_current, 5)

/* ----------------------------------------------------------------------------------- input debouncing */


#define SW_DOOR_FL_DEBOUNCE_MS 30u
static uint16_t sw_door_fl_stable_ms;
static bool sw_door_fl_state;
static bool sw_door_fl_last_raw;

bool sw_door_fl_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_door_fl_last_raw) {
        sw_door_fl_last_raw = raw;
        sw_door_fl_stable_ms = 0u;
        return sw_door_fl_state;
    }
    if (sw_door_fl_stable_ms < SW_DOOR_FL_DEBOUNCE_MS) {
        sw_door_fl_stable_ms = (uint16_t)(sw_door_fl_stable_ms + dt_ms);
        if (sw_door_fl_stable_ms >= SW_DOOR_FL_DEBOUNCE_MS && sw_door_fl_state != raw) {
            sw_door_fl_state = raw;
        }
    }
    return sw_door_fl_state;
}

void sw_door_fl_debounce_reset(void)
{
    sw_door_fl_stable_ms = 0u;
    sw_door_fl_state = false;
    sw_door_fl_last_raw = false;
}


#define SW_DOOR_FR_DEBOUNCE_MS 30u
static uint16_t sw_door_fr_stable_ms;
static bool sw_door_fr_state;
static bool sw_door_fr_last_raw;

bool sw_door_fr_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_door_fr_last_raw) {
        sw_door_fr_last_raw = raw;
        sw_door_fr_stable_ms = 0u;
        return sw_door_fr_state;
    }
    if (sw_door_fr_stable_ms < SW_DOOR_FR_DEBOUNCE_MS) {
        sw_door_fr_stable_ms = (uint16_t)(sw_door_fr_stable_ms + dt_ms);
        if (sw_door_fr_stable_ms >= SW_DOOR_FR_DEBOUNCE_MS && sw_door_fr_state != raw) {
            sw_door_fr_state = raw;
        }
    }
    return sw_door_fr_state;
}

void sw_door_fr_debounce_reset(void)
{
    sw_door_fr_stable_ms = 0u;
    sw_door_fr_state = false;
    sw_door_fr_last_raw = false;
}


#define SW_DOOR_RL_DEBOUNCE_MS 30u
static uint16_t sw_door_rl_stable_ms;
static bool sw_door_rl_state;
static bool sw_door_rl_last_raw;

bool sw_door_rl_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_door_rl_last_raw) {
        sw_door_rl_last_raw = raw;
        sw_door_rl_stable_ms = 0u;
        return sw_door_rl_state;
    }
    if (sw_door_rl_stable_ms < SW_DOOR_RL_DEBOUNCE_MS) {
        sw_door_rl_stable_ms = (uint16_t)(sw_door_rl_stable_ms + dt_ms);
        if (sw_door_rl_stable_ms >= SW_DOOR_RL_DEBOUNCE_MS && sw_door_rl_state != raw) {
            sw_door_rl_state = raw;
        }
    }
    return sw_door_rl_state;
}

void sw_door_rl_debounce_reset(void)
{
    sw_door_rl_stable_ms = 0u;
    sw_door_rl_state = false;
    sw_door_rl_last_raw = false;
}


#define SW_DOOR_RR_DEBOUNCE_MS 30u
static uint16_t sw_door_rr_stable_ms;
static bool sw_door_rr_state;
static bool sw_door_rr_last_raw;

bool sw_door_rr_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_door_rr_last_raw) {
        sw_door_rr_last_raw = raw;
        sw_door_rr_stable_ms = 0u;
        return sw_door_rr_state;
    }
    if (sw_door_rr_stable_ms < SW_DOOR_RR_DEBOUNCE_MS) {
        sw_door_rr_stable_ms = (uint16_t)(sw_door_rr_stable_ms + dt_ms);
        if (sw_door_rr_stable_ms >= SW_DOOR_RR_DEBOUNCE_MS && sw_door_rr_state != raw) {
            sw_door_rr_state = raw;
        }
    }
    return sw_door_rr_state;
}

void sw_door_rr_debounce_reset(void)
{
    sw_door_rr_stable_ms = 0u;
    sw_door_rr_state = false;
    sw_door_rr_last_raw = false;
}


#define SW_TRUNK_DEBOUNCE_MS 40u
static uint16_t sw_trunk_stable_ms;
static bool sw_trunk_state;
static bool sw_trunk_last_raw;

bool sw_trunk_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_trunk_last_raw) {
        sw_trunk_last_raw = raw;
        sw_trunk_stable_ms = 0u;
        return sw_trunk_state;
    }
    if (sw_trunk_stable_ms < SW_TRUNK_DEBOUNCE_MS) {
        sw_trunk_stable_ms = (uint16_t)(sw_trunk_stable_ms + dt_ms);
        if (sw_trunk_stable_ms >= SW_TRUNK_DEBOUNCE_MS && sw_trunk_state != raw) {
            sw_trunk_state = raw;
        }
    }
    return sw_trunk_state;
}

void sw_trunk_debounce_reset(void)
{
    sw_trunk_stable_ms = 0u;
    sw_trunk_state = false;
    sw_trunk_last_raw = false;
}


#define SW_HOOD_DEBOUNCE_MS 50u
static uint16_t sw_hood_stable_ms;
static bool sw_hood_state;
static bool sw_hood_last_raw;

bool sw_hood_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_hood_last_raw) {
        sw_hood_last_raw = raw;
        sw_hood_stable_ms = 0u;
        return sw_hood_state;
    }
    if (sw_hood_stable_ms < SW_HOOD_DEBOUNCE_MS) {
        sw_hood_stable_ms = (uint16_t)(sw_hood_stable_ms + dt_ms);
        if (sw_hood_stable_ms >= SW_HOOD_DEBOUNCE_MS && sw_hood_state != raw) {
            sw_hood_state = raw;
        }
    }
    return sw_hood_state;
}

void sw_hood_debounce_reset(void)
{
    sw_hood_stable_ms = 0u;
    sw_hood_state = false;
    sw_hood_last_raw = false;
}


#define SW_HAZARD_DEBOUNCE_MS 20u
static uint16_t sw_hazard_stable_ms;
static bool sw_hazard_state;
static bool sw_hazard_last_raw;

bool sw_hazard_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_hazard_last_raw) {
        sw_hazard_last_raw = raw;
        sw_hazard_stable_ms = 0u;
        return sw_hazard_state;
    }
    if (sw_hazard_stable_ms < SW_HAZARD_DEBOUNCE_MS) {
        sw_hazard_stable_ms = (uint16_t)(sw_hazard_stable_ms + dt_ms);
        if (sw_hazard_stable_ms >= SW_HAZARD_DEBOUNCE_MS && sw_hazard_state != raw) {
            sw_hazard_state = raw;
        }
    }
    return sw_hazard_state;
}

void sw_hazard_debounce_reset(void)
{
    sw_hazard_stable_ms = 0u;
    sw_hazard_state = false;
    sw_hazard_last_raw = false;
}


#define SW_LOCK_DEBOUNCE_MS 25u
static uint16_t sw_lock_stable_ms;
static bool sw_lock_state;
static bool sw_lock_last_raw;

bool sw_lock_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_lock_last_raw) {
        sw_lock_last_raw = raw;
        sw_lock_stable_ms = 0u;
        return sw_lock_state;
    }
    if (sw_lock_stable_ms < SW_LOCK_DEBOUNCE_MS) {
        sw_lock_stable_ms = (uint16_t)(sw_lock_stable_ms + dt_ms);
        if (sw_lock_stable_ms >= SW_LOCK_DEBOUNCE_MS && sw_lock_state != raw) {
            sw_lock_state = raw;
        }
    }
    return sw_lock_state;
}

void sw_lock_debounce_reset(void)
{
    sw_lock_stable_ms = 0u;
    sw_lock_state = false;
    sw_lock_last_raw = false;
}


#define SW_UNLOCK_DEBOUNCE_MS 25u
static uint16_t sw_unlock_stable_ms;
static bool sw_unlock_state;
static bool sw_unlock_last_raw;

bool sw_unlock_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_unlock_last_raw) {
        sw_unlock_last_raw = raw;
        sw_unlock_stable_ms = 0u;
        return sw_unlock_state;
    }
    if (sw_unlock_stable_ms < SW_UNLOCK_DEBOUNCE_MS) {
        sw_unlock_stable_ms = (uint16_t)(sw_unlock_stable_ms + dt_ms);
        if (sw_unlock_stable_ms >= SW_UNLOCK_DEBOUNCE_MS && sw_unlock_state != raw) {
            sw_unlock_state = raw;
        }
    }
    return sw_unlock_state;
}

void sw_unlock_debounce_reset(void)
{
    sw_unlock_stable_ms = 0u;
    sw_unlock_state = false;
    sw_unlock_last_raw = false;
}


#define SW_HORN_DEBOUNCE_MS 10u
static uint16_t sw_horn_stable_ms;
static bool sw_horn_state;
static bool sw_horn_last_raw;

bool sw_horn_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_horn_last_raw) {
        sw_horn_last_raw = raw;
        sw_horn_stable_ms = 0u;
        return sw_horn_state;
    }
    if (sw_horn_stable_ms < SW_HORN_DEBOUNCE_MS) {
        sw_horn_stable_ms = (uint16_t)(sw_horn_stable_ms + dt_ms);
        if (sw_horn_stable_ms >= SW_HORN_DEBOUNCE_MS && sw_horn_state != raw) {
            sw_horn_state = raw;
        }
    }
    return sw_horn_state;
}

void sw_horn_debounce_reset(void)
{
    sw_horn_stable_ms = 0u;
    sw_horn_state = false;
    sw_horn_last_raw = false;
}


#define SW_BRAKE_DEBOUNCE_MS 15u
static uint16_t sw_brake_stable_ms;
static bool sw_brake_state;
static bool sw_brake_last_raw;

bool sw_brake_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_brake_last_raw) {
        sw_brake_last_raw = raw;
        sw_brake_stable_ms = 0u;
        return sw_brake_state;
    }
    if (sw_brake_stable_ms < SW_BRAKE_DEBOUNCE_MS) {
        sw_brake_stable_ms = (uint16_t)(sw_brake_stable_ms + dt_ms);
        if (sw_brake_stable_ms >= SW_BRAKE_DEBOUNCE_MS && sw_brake_state != raw) {
            sw_brake_state = raw;
        }
    }
    return sw_brake_state;
}

void sw_brake_debounce_reset(void)
{
    sw_brake_stable_ms = 0u;
    sw_brake_state = false;
    sw_brake_last_raw = false;
}


#define SW_REVERSE_DEBOUNCE_MS 20u
static uint16_t sw_reverse_stable_ms;
static bool sw_reverse_state;
static bool sw_reverse_last_raw;

bool sw_reverse_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_reverse_last_raw) {
        sw_reverse_last_raw = raw;
        sw_reverse_stable_ms = 0u;
        return sw_reverse_state;
    }
    if (sw_reverse_stable_ms < SW_REVERSE_DEBOUNCE_MS) {
        sw_reverse_stable_ms = (uint16_t)(sw_reverse_stable_ms + dt_ms);
        if (sw_reverse_stable_ms >= SW_REVERSE_DEBOUNCE_MS && sw_reverse_state != raw) {
            sw_reverse_state = raw;
        }
    }
    return sw_reverse_state;
}

void sw_reverse_debounce_reset(void)
{
    sw_reverse_stable_ms = 0u;
    sw_reverse_state = false;
    sw_reverse_last_raw = false;
}


#define SW_FOG_FRONT_DEBOUNCE_MS 20u
static uint16_t sw_fog_front_stable_ms;
static bool sw_fog_front_state;
static bool sw_fog_front_last_raw;

bool sw_fog_front_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_fog_front_last_raw) {
        sw_fog_front_last_raw = raw;
        sw_fog_front_stable_ms = 0u;
        return sw_fog_front_state;
    }
    if (sw_fog_front_stable_ms < SW_FOG_FRONT_DEBOUNCE_MS) {
        sw_fog_front_stable_ms = (uint16_t)(sw_fog_front_stable_ms + dt_ms);
        if (sw_fog_front_stable_ms >= SW_FOG_FRONT_DEBOUNCE_MS && sw_fog_front_state != raw) {
            sw_fog_front_state = raw;
        }
    }
    return sw_fog_front_state;
}

void sw_fog_front_debounce_reset(void)
{
    sw_fog_front_stable_ms = 0u;
    sw_fog_front_state = false;
    sw_fog_front_last_raw = false;
}


#define SW_FOG_REAR_DEBOUNCE_MS 20u
static uint16_t sw_fog_rear_stable_ms;
static bool sw_fog_rear_state;
static bool sw_fog_rear_last_raw;

bool sw_fog_rear_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_fog_rear_last_raw) {
        sw_fog_rear_last_raw = raw;
        sw_fog_rear_stable_ms = 0u;
        return sw_fog_rear_state;
    }
    if (sw_fog_rear_stable_ms < SW_FOG_REAR_DEBOUNCE_MS) {
        sw_fog_rear_stable_ms = (uint16_t)(sw_fog_rear_stable_ms + dt_ms);
        if (sw_fog_rear_stable_ms >= SW_FOG_REAR_DEBOUNCE_MS && sw_fog_rear_state != raw) {
            sw_fog_rear_state = raw;
        }
    }
    return sw_fog_rear_state;
}

void sw_fog_rear_debounce_reset(void)
{
    sw_fog_rear_stable_ms = 0u;
    sw_fog_rear_state = false;
    sw_fog_rear_last_raw = false;
}


#define SW_TAILGATE_DEBOUNCE_MS 30u
static uint16_t sw_tailgate_stable_ms;
static bool sw_tailgate_state;
static bool sw_tailgate_last_raw;

bool sw_tailgate_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_tailgate_last_raw) {
        sw_tailgate_last_raw = raw;
        sw_tailgate_stable_ms = 0u;
        return sw_tailgate_state;
    }
    if (sw_tailgate_stable_ms < SW_TAILGATE_DEBOUNCE_MS) {
        sw_tailgate_stable_ms = (uint16_t)(sw_tailgate_stable_ms + dt_ms);
        if (sw_tailgate_stable_ms >= SW_TAILGATE_DEBOUNCE_MS && sw_tailgate_state != raw) {
            sw_tailgate_state = raw;
        }
    }
    return sw_tailgate_state;
}

void sw_tailgate_debounce_reset(void)
{
    sw_tailgate_stable_ms = 0u;
    sw_tailgate_state = false;
    sw_tailgate_last_raw = false;
}


#define SW_SUNROOF_OPEN_DEBOUNCE_MS 20u
static uint16_t sw_sunroof_open_stable_ms;
static bool sw_sunroof_open_state;
static bool sw_sunroof_open_last_raw;

bool sw_sunroof_open_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_sunroof_open_last_raw) {
        sw_sunroof_open_last_raw = raw;
        sw_sunroof_open_stable_ms = 0u;
        return sw_sunroof_open_state;
    }
    if (sw_sunroof_open_stable_ms < SW_SUNROOF_OPEN_DEBOUNCE_MS) {
        sw_sunroof_open_stable_ms = (uint16_t)(sw_sunroof_open_stable_ms + dt_ms);
        if (sw_sunroof_open_stable_ms >= SW_SUNROOF_OPEN_DEBOUNCE_MS && sw_sunroof_open_state != raw) {
            sw_sunroof_open_state = raw;
        }
    }
    return sw_sunroof_open_state;
}

void sw_sunroof_open_debounce_reset(void)
{
    sw_sunroof_open_stable_ms = 0u;
    sw_sunroof_open_state = false;
    sw_sunroof_open_last_raw = false;
}


#define SW_SUNROOF_CLOSE_DEBOUNCE_MS 20u
static uint16_t sw_sunroof_close_stable_ms;
static bool sw_sunroof_close_state;
static bool sw_sunroof_close_last_raw;

bool sw_sunroof_close_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_sunroof_close_last_raw) {
        sw_sunroof_close_last_raw = raw;
        sw_sunroof_close_stable_ms = 0u;
        return sw_sunroof_close_state;
    }
    if (sw_sunroof_close_stable_ms < SW_SUNROOF_CLOSE_DEBOUNCE_MS) {
        sw_sunroof_close_stable_ms = (uint16_t)(sw_sunroof_close_stable_ms + dt_ms);
        if (sw_sunroof_close_stable_ms >= SW_SUNROOF_CLOSE_DEBOUNCE_MS && sw_sunroof_close_state != raw) {
            sw_sunroof_close_state = raw;
        }
    }
    return sw_sunroof_close_state;
}

void sw_sunroof_close_debounce_reset(void)
{
    sw_sunroof_close_stable_ms = 0u;
    sw_sunroof_close_state = false;
    sw_sunroof_close_last_raw = false;
}


#define SW_KICK_DEBOUNCE_MS 60u
static uint16_t sw_kick_stable_ms;
static bool sw_kick_state;
static bool sw_kick_last_raw;

bool sw_kick_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_kick_last_raw) {
        sw_kick_last_raw = raw;
        sw_kick_stable_ms = 0u;
        return sw_kick_state;
    }
    if (sw_kick_stable_ms < SW_KICK_DEBOUNCE_MS) {
        sw_kick_stable_ms = (uint16_t)(sw_kick_stable_ms + dt_ms);
        if (sw_kick_stable_ms >= SW_KICK_DEBOUNCE_MS && sw_kick_state != raw) {
            sw_kick_state = raw;
        }
    }
    return sw_kick_state;
}

void sw_kick_debounce_reset(void)
{
    sw_kick_stable_ms = 0u;
    sw_kick_state = false;
    sw_kick_last_raw = false;
}


#define SW_PANIC_DEBOUNCE_MS 40u
static uint16_t sw_panic_stable_ms;
static bool sw_panic_state;
static bool sw_panic_last_raw;

bool sw_panic_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_panic_last_raw) {
        sw_panic_last_raw = raw;
        sw_panic_stable_ms = 0u;
        return sw_panic_state;
    }
    if (sw_panic_stable_ms < SW_PANIC_DEBOUNCE_MS) {
        sw_panic_stable_ms = (uint16_t)(sw_panic_stable_ms + dt_ms);
        if (sw_panic_stable_ms >= SW_PANIC_DEBOUNCE_MS && sw_panic_state != raw) {
            sw_panic_state = raw;
        }
    }
    return sw_panic_state;
}

void sw_panic_debounce_reset(void)
{
    sw_panic_stable_ms = 0u;
    sw_panic_state = false;
    sw_panic_last_raw = false;
}


#define SW_SEATBELT_DEBOUNCE_MS 50u
static uint16_t sw_seatbelt_stable_ms;
static bool sw_seatbelt_state;
static bool sw_seatbelt_last_raw;

bool sw_seatbelt_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_seatbelt_last_raw) {
        sw_seatbelt_last_raw = raw;
        sw_seatbelt_stable_ms = 0u;
        return sw_seatbelt_state;
    }
    if (sw_seatbelt_stable_ms < SW_SEATBELT_DEBOUNCE_MS) {
        sw_seatbelt_stable_ms = (uint16_t)(sw_seatbelt_stable_ms + dt_ms);
        if (sw_seatbelt_stable_ms >= SW_SEATBELT_DEBOUNCE_MS && sw_seatbelt_state != raw) {
            sw_seatbelt_state = raw;
        }
    }
    return sw_seatbelt_state;
}

void sw_seatbelt_debounce_reset(void)
{
    sw_seatbelt_stable_ms = 0u;
    sw_seatbelt_state = false;
    sw_seatbelt_last_raw = false;
}


#define SW_REAR_DEFROST_DEBOUNCE_MS 20u
static uint16_t sw_rear_defrost_stable_ms;
static bool sw_rear_defrost_state;
static bool sw_rear_defrost_last_raw;

bool sw_rear_defrost_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_rear_defrost_last_raw) {
        sw_rear_defrost_last_raw = raw;
        sw_rear_defrost_stable_ms = 0u;
        return sw_rear_defrost_state;
    }
    if (sw_rear_defrost_stable_ms < SW_REAR_DEFROST_DEBOUNCE_MS) {
        sw_rear_defrost_stable_ms = (uint16_t)(sw_rear_defrost_stable_ms + dt_ms);
        if (sw_rear_defrost_stable_ms >= SW_REAR_DEFROST_DEBOUNCE_MS && sw_rear_defrost_state != raw) {
            sw_rear_defrost_state = raw;
        }
    }
    return sw_rear_defrost_state;
}

void sw_rear_defrost_debounce_reset(void)
{
    sw_rear_defrost_stable_ms = 0u;
    sw_rear_defrost_state = false;
    sw_rear_defrost_last_raw = false;
}


#define SW_CHILD_LOCK_DEBOUNCE_MS 30u
static uint16_t sw_child_lock_stable_ms;
static bool sw_child_lock_state;
static bool sw_child_lock_last_raw;

bool sw_child_lock_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_child_lock_last_raw) {
        sw_child_lock_last_raw = raw;
        sw_child_lock_stable_ms = 0u;
        return sw_child_lock_state;
    }
    if (sw_child_lock_stable_ms < SW_CHILD_LOCK_DEBOUNCE_MS) {
        sw_child_lock_stable_ms = (uint16_t)(sw_child_lock_stable_ms + dt_ms);
        if (sw_child_lock_stable_ms >= SW_CHILD_LOCK_DEBOUNCE_MS && sw_child_lock_state != raw) {
            sw_child_lock_state = raw;
        }
    }
    return sw_child_lock_state;
}

void sw_child_lock_debounce_reset(void)
{
    sw_child_lock_stable_ms = 0u;
    sw_child_lock_state = false;
    sw_child_lock_last_raw = false;
}


#define SW_AC_DEBOUNCE_MS 20u
static uint16_t sw_ac_stable_ms;
static bool sw_ac_state;
static bool sw_ac_last_raw;

bool sw_ac_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_ac_last_raw) {
        sw_ac_last_raw = raw;
        sw_ac_stable_ms = 0u;
        return sw_ac_state;
    }
    if (sw_ac_stable_ms < SW_AC_DEBOUNCE_MS) {
        sw_ac_stable_ms = (uint16_t)(sw_ac_stable_ms + dt_ms);
        if (sw_ac_stable_ms >= SW_AC_DEBOUNCE_MS && sw_ac_state != raw) {
            sw_ac_state = raw;
        }
    }
    return sw_ac_state;
}

void sw_ac_debounce_reset(void)
{
    sw_ac_stable_ms = 0u;
    sw_ac_state = false;
    sw_ac_last_raw = false;
}


#define SW_RECIRC_DEBOUNCE_MS 20u
static uint16_t sw_recirc_stable_ms;
static bool sw_recirc_state;
static bool sw_recirc_last_raw;

bool sw_recirc_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_recirc_last_raw) {
        sw_recirc_last_raw = raw;
        sw_recirc_stable_ms = 0u;
        return sw_recirc_state;
    }
    if (sw_recirc_stable_ms < SW_RECIRC_DEBOUNCE_MS) {
        sw_recirc_stable_ms = (uint16_t)(sw_recirc_stable_ms + dt_ms);
        if (sw_recirc_stable_ms >= SW_RECIRC_DEBOUNCE_MS && sw_recirc_state != raw) {
            sw_recirc_state = raw;
        }
    }
    return sw_recirc_state;
}

void sw_recirc_debounce_reset(void)
{
    sw_recirc_stable_ms = 0u;
    sw_recirc_state = false;
    sw_recirc_last_raw = false;
}


#define SW_FUEL_LID_DEBOUNCE_MS 40u
static uint16_t sw_fuel_lid_stable_ms;
static bool sw_fuel_lid_state;
static bool sw_fuel_lid_last_raw;

bool sw_fuel_lid_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_fuel_lid_last_raw) {
        sw_fuel_lid_last_raw = raw;
        sw_fuel_lid_stable_ms = 0u;
        return sw_fuel_lid_state;
    }
    if (sw_fuel_lid_stable_ms < SW_FUEL_LID_DEBOUNCE_MS) {
        sw_fuel_lid_stable_ms = (uint16_t)(sw_fuel_lid_stable_ms + dt_ms);
        if (sw_fuel_lid_stable_ms >= SW_FUEL_LID_DEBOUNCE_MS && sw_fuel_lid_state != raw) {
            sw_fuel_lid_state = raw;
        }
    }
    return sw_fuel_lid_state;
}

void sw_fuel_lid_debounce_reset(void)
{
    sw_fuel_lid_stable_ms = 0u;
    sw_fuel_lid_state = false;
    sw_fuel_lid_last_raw = false;
}


#define SW_PARKING_BRAKE_DEBOUNCE_MS 30u
static uint16_t sw_parking_brake_stable_ms;
static bool sw_parking_brake_state;
static bool sw_parking_brake_last_raw;

bool sw_parking_brake_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_parking_brake_last_raw) {
        sw_parking_brake_last_raw = raw;
        sw_parking_brake_stable_ms = 0u;
        return sw_parking_brake_state;
    }
    if (sw_parking_brake_stable_ms < SW_PARKING_BRAKE_DEBOUNCE_MS) {
        sw_parking_brake_stable_ms = (uint16_t)(sw_parking_brake_stable_ms + dt_ms);
        if (sw_parking_brake_stable_ms >= SW_PARKING_BRAKE_DEBOUNCE_MS && sw_parking_brake_state != raw) {
            sw_parking_brake_state = raw;
        }
    }
    return sw_parking_brake_state;
}

void sw_parking_brake_debounce_reset(void)
{
    sw_parking_brake_stable_ms = 0u;
    sw_parking_brake_state = false;
    sw_parking_brake_last_raw = false;
}


#define SW_MIRROR_FOLD_DEBOUNCE_MS 20u
static uint16_t sw_mirror_fold_stable_ms;
static bool sw_mirror_fold_state;
static bool sw_mirror_fold_last_raw;

bool sw_mirror_fold_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_mirror_fold_last_raw) {
        sw_mirror_fold_last_raw = raw;
        sw_mirror_fold_stable_ms = 0u;
        return sw_mirror_fold_state;
    }
    if (sw_mirror_fold_stable_ms < SW_MIRROR_FOLD_DEBOUNCE_MS) {
        sw_mirror_fold_stable_ms = (uint16_t)(sw_mirror_fold_stable_ms + dt_ms);
        if (sw_mirror_fold_stable_ms >= SW_MIRROR_FOLD_DEBOUNCE_MS && sw_mirror_fold_state != raw) {
            sw_mirror_fold_state = raw;
        }
    }
    return sw_mirror_fold_state;
}

void sw_mirror_fold_debounce_reset(void)
{
    sw_mirror_fold_stable_ms = 0u;
    sw_mirror_fold_state = false;
    sw_mirror_fold_last_raw = false;
}


#define SW_SLIDE_L_DEBOUNCE_MS 30u
static uint16_t sw_slide_l_stable_ms;
static bool sw_slide_l_state;
static bool sw_slide_l_last_raw;

bool sw_slide_l_debounce(bool raw, uint16_t dt_ms)
{
    if (raw != sw_slide_l_last_raw) {
        sw_slide_l_last_raw = raw;
        sw_slide_l_stable_ms = 0u;
        return sw_slide_l_state;
    }
    if (sw_slide_l_stable_ms < SW_SLIDE_L_DEBOUNCE_MS) {
        sw_slide_l_stable_ms = (uint16_t)(sw_slide_l_stable_ms + dt_ms);
        if (sw_slide_l_stable_ms >= SW_SLIDE_L_DEBOUNCE_MS && sw_slide_l_state != raw) {
            sw_slide_l_state = raw;
        }
    }
    return sw_slide_l_state;
}

void sw_slide_l_debounce_reset(void)
{
    sw_slide_l_stable_ms = 0u;
    sw_slide_l_state = false;
    sw_slide_l_last_raw = false;
}


/* --------------------------------------------------------------------------------------- power modes */
static bcm_power_t g_power = BCM_POWER_OFF;
static uint16_t g_crank_ms;

bcm_power_t bcm_power_mode(void)
{
    return g_power;
}

bcm_power_t bcm_power_step(bcm_key_t key, bool brake, bool engine_running, uint16_t dt_ms)
{
    switch (g_power) {
    case BCM_POWER_OFF:
        if (key == BCM_KEY_ACC) {
            g_power = BCM_POWER_ACC;
        } else if (key == BCM_KEY_RUN || key == BCM_KEY_START) {
            g_power = BCM_POWER_RUN;
        }
        break;
    case BCM_POWER_ACC:
        if (key == BCM_KEY_OFF) {
            g_power = BCM_POWER_OFF;
        } else if (key == BCM_KEY_RUN) {
            g_power = BCM_POWER_RUN;
        } else if (key == BCM_KEY_START && brake) {
            g_power = BCM_POWER_CRANK;
            g_crank_ms = 0u;
        }
        break;
    case BCM_POWER_RUN:
        if (key == BCM_KEY_OFF) {
            g_power = BCM_POWER_OFF;
        } else if (key == BCM_KEY_ACC && !engine_running) {
            g_power = BCM_POWER_ACC;
        } else if (key == BCM_KEY_START && brake && !engine_running) {
            g_power = BCM_POWER_CRANK;
            g_crank_ms = 0u;
        }
        break;
    case BCM_POWER_CRANK:
        g_crank_ms = (uint16_t)(g_crank_ms + dt_ms);
        if (engine_running) {
            g_power = BCM_POWER_RUN;
        } else if (key != BCM_KEY_START || g_crank_ms > BCM_CRANK_MAX_MS) {
            g_power = BCM_POWER_RUN;
            bcm_dtc_report(BCM_DTC_CRANK_TIMEOUT, g_crank_ms > BCM_CRANK_MAX_MS);
        }
        break;
    default:
        g_power = BCM_POWER_OFF;
        break;
    }
    return g_power;
}

void bcm_power_reset(void)
{
    g_power = BCM_POWER_OFF;
    g_crank_ms = 0u;
}


/* ------------------------------------------------------------------------------------ exterior lights */
static bool g_low_beam;
static bool g_auto_on;
static uint16_t g_dark_ms;
static uint16_t g_bright_ms;
static uint16_t g_follow_me_ms;

bool bcm_lights_auto(int32_t lux, bool auto_switch, bool wipers_fast, bcm_power_t power, uint16_t dt_ms)
{
    int32_t filtered = ema_light_lux_update(lux);
    if (power != BCM_POWER_RUN || !auto_switch) {
        g_auto_on = false;
        g_dark_ms = 0u;
        g_bright_ms = 0u;
        return false;
    }
    if (wipers_fast) {
        g_auto_on = true;
        return true;
    }
    if (filtered < BCM_LUX_ON) {
        g_bright_ms = 0u;
        if (g_dark_ms < BCM_LIGHT_ON_DELAY_MS) {
            g_dark_ms = (uint16_t)(g_dark_ms + dt_ms);
        } else {
            g_auto_on = true;
        }
    } else if (filtered > BCM_LUX_OFF) {
        g_dark_ms = 0u;
        if (g_bright_ms < BCM_LIGHT_OFF_DELAY_MS) {
            g_bright_ms = (uint16_t)(g_bright_ms + dt_ms);
        } else {
            g_auto_on = false;
        }
    }
    return g_auto_on;
}

bool bcm_low_beam(bcm_light_switch_t sw, bool auto_on, bool driver_door_open, bcm_power_t power, uint16_t dt_ms)
{
    switch (sw) {
    case BCM_LIGHT_ON:
        g_low_beam = power != BCM_POWER_OFF;
        g_follow_me_ms = 0u;
        break;
    case BCM_LIGHT_AUTO:
        if (power == BCM_POWER_RUN) {
            g_low_beam = auto_on;
            g_follow_me_ms = auto_on ? BCM_FOLLOW_ME_MS : g_follow_me_ms;
        } else if (g_follow_me_ms > 0u && !driver_door_open) {
            g_follow_me_ms = g_follow_me_ms > dt_ms ? (uint16_t)(g_follow_me_ms - dt_ms) : 0u;
            g_low_beam = g_follow_me_ms > 0u;
        } else {
            g_low_beam = g_follow_me_ms > 0u && driver_door_open;
        }
        break;
    case BCM_LIGHT_OFF:
    default:
        g_low_beam = false;
        g_follow_me_ms = 0u;
        break;
    }
    hal_gpio_write(BCM_PIN_LOW_BEAM, g_low_beam);
    return g_low_beam;
}

uint8_t bcm_bulb_check(uint16_t supply_mv, uint16_t measured_ma, bool commanded)
{
    int16_t expected;
    int32_t low;
    int32_t high;
    if (!commanded) {
        return measured_ma > BCM_BULB_LEAK_MA ? BCM_BULB_SHORT : BCM_BULB_OK;
    }
    expected = lut_bulb_current_lookup((int16_t)clamp_u16(supply_mv, 9000u, 14500u));
    low = (int32_t)expected * 60 / 100;
    high = (int32_t)expected * 150 / 100;
    if ((int32_t)measured_ma < low) {
        return BCM_BULB_OPEN;
    }
    if ((int32_t)measured_ma > high) {
        return BCM_BULB_SHORT;
    }
    return BCM_BULB_OK;
}

void bcm_lights_reset(void)
{
    g_low_beam = false;
    g_auto_on = false;
    g_dark_ms = 0u;
    g_bright_ms = 0u;
    g_follow_me_ms = 0u;
    ema_light_lux_reset();
}


/* ----------------------------------------------------------------------------------- turn indicators */
static bcm_turn_t g_turn_req;
static uint8_t g_comfort_left;
static uint16_t g_flash_ms;
static bool g_flash_on;

bcm_turn_t bcm_turn_request(bcm_turn_t lever, bool hazard, bool tap, bool crash)
{
    if (crash || hazard) {
        g_turn_req = BCM_TURN_HAZARD;
        g_comfort_left = 0u;
    } else if (lever == BCM_TURN_LEFT || lever == BCM_TURN_RIGHT) {
        g_turn_req = lever;
        g_comfort_left = tap ? BCM_COMFORT_FLASHES : 0u;
    } else if (g_comfort_left > 0u) {
        /* comfort flashing continues until its flashes are done */
    } else {
        g_turn_req = BCM_TURN_NONE;
    }
    return g_turn_req;
}

bool bcm_turn_flash(uint16_t dt_ms, bool bulb_failed)
{
    uint16_t period = bulb_failed ? BCM_FLASH_FAST_MS : BCM_FLASH_MS;
    if (g_turn_req == BCM_TURN_NONE) {
        g_flash_ms = 0u;
        g_flash_on = false;
        return false;
    }
    g_flash_ms = (uint16_t)(g_flash_ms + dt_ms);
    if (g_flash_ms >= period / 2u) {
        g_flash_ms = 0u;
        g_flash_on = !g_flash_on;
        if (!g_flash_on && g_comfort_left > 0u) {
            g_comfort_left--;
            if (g_comfort_left == 0u && g_turn_req != BCM_TURN_HAZARD) {
                g_turn_req = BCM_TURN_NONE;
            }
        }
    }
    hal_gpio_write(BCM_PIN_TURN_LEFT, g_flash_on && (g_turn_req == BCM_TURN_LEFT || g_turn_req == BCM_TURN_HAZARD));
    hal_gpio_write(BCM_PIN_TURN_RIGHT, g_flash_on && (g_turn_req == BCM_TURN_RIGHT || g_turn_req == BCM_TURN_HAZARD));
    return g_flash_on;
}

void bcm_turn_reset(void)
{
    g_turn_req = BCM_TURN_NONE;
    g_comfort_left = 0u;
    g_flash_ms = 0u;
    g_flash_on = false;
}


/* ------------------------------------------------------------------------------------ front wiper */
static uint16_t front_since_ms;
static bool front_parking;

static bool front_interval_due(uint16_t interval, uint16_t dt_ms)
{
    front_since_ms = (uint16_t)(front_since_ms + dt_ms);
    if (front_since_ms >= interval) {
        front_since_ms = 0u;
        return true;
    }
    return false;
}

uint8_t bcm_wiper_front(bcm_wiper_mode_t mode, int16_t speed_kph, int16_t rain_level, bool washer, uint16_t dt_ms)
{
    uint8_t duty = 0u;
    uint16_t interval;
    int16_t level;
    (void)level;
    (void)rain_level;
    interval = (uint16_t)lut_wiper_interval_lookup(speed_kph);
    if (washer) {
        front_parking = false;
        hal_pwm_set(BCM_PWM_WIPER_F, BCM_WIPER_LOW_DUTY);
        return BCM_WIPER_LOW_DUTY;
    }
    switch (mode) {
    case BCM_WIPER_OFF:
        duty = 0u;
        break;
    case BCM_WIPER_INTERVAL:
        duty = front_interval_due(interval, dt_ms) ? BCM_WIPER_LOW_DUTY : 0u;
        break;
    case BCM_WIPER_LOW:
        duty = BCM_WIPER_LOW_DUTY;
        break;
    case BCM_WIPER_HIGH:
        duty = speed_kph < 5 ? BCM_WIPER_LOW_DUTY : BCM_WIPER_HIGH_DUTY;
        break;
    case BCM_WIPER_AUTO:
        level = ema_rain_level_update(rain_level);
        if (level >= BCM_RAIN_HEAVY) {
            duty = BCM_WIPER_HIGH_DUTY;
        } else if (level >= BCM_RAIN_LIGHT) {
            duty = BCM_WIPER_LOW_DUTY;
        } else if (level > 0) {
            interval = (uint16_t)(interval * 2u);
            duty = front_interval_due(interval, dt_ms) ? BCM_WIPER_LOW_DUTY : 0u;
        } else {
            duty = 0u;
        }
        break;
    default:
        duty = 0u;
        break;
    }
    if (duty == 0u && !hal_gpio_read(BCM_PIN_WIPER_PARK_F)) {
        front_parking = true;
        duty = BCM_WIPER_PARK_DUTY;
    } else if (duty == 0u) {
        front_parking = false;
    }
    hal_pwm_set(BCM_PWM_WIPER_F, duty);
    return duty;
}

bool bcm_wiper_front_parking(void)
{
    return front_parking;
}

void bcm_wiper_front_reset(void)
{
    front_since_ms = 0u;
    front_parking = false;
}


/* ------------------------------------------------------------------------------------ rear wiper */
static uint16_t rear_since_ms;
static bool rear_parking;

static bool rear_interval_due(uint16_t interval, uint16_t dt_ms)
{
    rear_since_ms = (uint16_t)(rear_since_ms + dt_ms);
    if (rear_since_ms >= interval) {
        rear_since_ms = 0u;
        return true;
    }
    return false;
}

uint8_t bcm_wiper_rear(bcm_wiper_mode_t mode, int16_t speed_kph, int16_t rain_level, bool washer, uint16_t dt_ms)
{
    uint8_t duty = 0u;
    uint16_t interval;
    int16_t level;
    (void)level;
    (void)rain_level;
    interval = speed_kph > 60 ? 6000u : 10000u;
    if (washer) {
        rear_parking = false;
        hal_pwm_set(BCM_PWM_WIPER_R, BCM_WIPER_LOW_DUTY);
        return BCM_WIPER_LOW_DUTY;
    }
    switch (mode) {
    case BCM_WIPER_OFF:
        duty = 0u;
        break;
    case BCM_WIPER_INTERVAL:
        duty = rear_interval_due(interval, dt_ms) ? BCM_WIPER_LOW_DUTY : 0u;
        break;
    case BCM_WIPER_LOW:
        duty = BCM_WIPER_LOW_DUTY;
        break;
    case BCM_WIPER_HIGH:
        duty = speed_kph < 5 ? BCM_WIPER_LOW_DUTY : BCM_WIPER_HIGH_DUTY;
        break;
    default:
        duty = 0u;
        break;
    }
    if (duty == 0u && !hal_gpio_read(BCM_PIN_WIPER_PARK_R)) {
        rear_parking = true;
        duty = BCM_WIPER_PARK_DUTY;
    } else if (duty == 0u) {
        rear_parking = false;
    }
    hal_pwm_set(BCM_PWM_WIPER_R, duty);
    return duty;
}

bool bcm_wiper_rear_parking(void)
{
    return rear_parking;
}

void bcm_wiper_rear_reset(void)
{
    rear_since_ms = 0u;
    rear_parking = false;
}


/* ----------------------------------------------------------------------------------- central locking */
static bool g_locked;
static bool g_auto_locked;
static bool g_child_lock;
static uint8_t g_lock_cycles;
static uint16_t g_lock_window_ms;

bool bcm_lock_command(bcm_lock_cmd_t cmd, int16_t speed_kph, bool crash, bool any_door_open, uint16_t dt_ms)
{
    if (crash) {
        g_locked = false;
        g_auto_locked = false;
        hal_gpio_write(BCM_PIN_UNLOCK_ALL, true);
        return false;
    }
    g_lock_window_ms = (uint16_t)(g_lock_window_ms + dt_ms);
    if (g_lock_window_ms > BCM_LOCK_WINDOW_MS) {
        g_lock_window_ms = 0u;
        g_lock_cycles = 0u;
    }
    if (cmd == BCM_LOCK || cmd == BCM_UNLOCK) {
        g_lock_cycles++;
        if (g_lock_cycles > BCM_LOCK_MAX_CYCLES) {
            bcm_dtc_report(BCM_DTC_LOCK_OVERUSE, true);
            return g_locked;
        }
    }
    switch (cmd) {
    case BCM_LOCK:
        if (!any_door_open) {
            g_locked = true;
        }
        break;
    case BCM_UNLOCK:
        g_locked = false;
        g_auto_locked = false;
        break;
    case BCM_LOCK_NONE:
    default:
        if (!g_locked && !g_auto_locked && !any_door_open && speed_kph >= BCM_AUTOLOCK_KPH) {
            g_locked = true;
            g_auto_locked = true;
        }
        break;
    }
    hal_gpio_write(BCM_PIN_LOCK_ALL, g_locked);
    return g_locked;
}

bool bcm_child_lock(bool request, bool rear_door_open)
{
    if (rear_door_open) {
        g_child_lock = request;
    }
    return g_child_lock;
}

bool bcm_inner_handle_opens(bool rear, bool locked)
{
    if (rear && g_child_lock) {
        return false;
    }
    return !locked || !rear;
}

void bcm_lock_reset(void)
{
    g_locked = false;
    g_auto_locked = false;
    g_child_lock = false;
    g_lock_cycles = 0u;
    g_lock_window_ms = 0u;
}


/* --------------------------------------------------------------------------------- window fl */
#define WIN_FL_CURRENT_LIMIT_MA 9000
static int16_t win_fl_pos;          /* 0 closed .. 1000 open, tenths of a percent */
static bcm_win_state_t win_fl_state;
static uint16_t win_fl_reverse_ms;
static uint8_t win_fl_pinches;

bcm_win_state_t bcm_window_fl(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms)
{
    uint16_t current = hal_adc_read(BCM_ADC_WIN_FL);
    bool pinch = win_fl_state == BCM_WIN_CLOSING && current > WIN_FL_CURRENT_LIMIT_MA && win_fl_pos > BCM_WIN_PINCH_ZONE;
    if (win_fl_state == BCM_WIN_REVERSING) {
        win_fl_reverse_ms = (uint16_t)(win_fl_reverse_ms + dt_ms);
        win_fl_pos = clamp_i16((int16_t)(win_fl_pos + 20), 0, 1000);
        if (win_fl_reverse_ms >= BCM_WIN_REVERSE_MS || win_fl_pos >= 1000) {
            win_fl_state = BCM_WIN_STOPPED;
            win_fl_reverse_ms = 0u;
        }
    } else if (pinch) {
        win_fl_state = BCM_WIN_REVERSING;
        win_fl_reverse_ms = 0u;
        win_fl_pinches++;
        if (win_fl_pinches >= BCM_WIN_MAX_PINCHES) {
            bcm_dtc_report(BCM_DTC_WINDOW_PINCH, true);
        }
    } else if (cmd == BCM_WIN_UP && win_fl_pos > 0) {
        win_fl_state = BCM_WIN_CLOSING;
        win_fl_pos = clamp_i16((int16_t)(win_fl_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_DOWN && win_fl_pos < 1000) {
        win_fl_state = BCM_WIN_OPENING;
        win_fl_pos = clamp_i16((int16_t)(win_fl_pos + 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_fl_state == BCM_WIN_CLOSING && win_fl_pos > 0) {
        win_fl_pos = clamp_i16((int16_t)(win_fl_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_fl_state == BCM_WIN_OPENING && win_fl_pos < 1000) {
        win_fl_pos = clamp_i16((int16_t)(win_fl_pos + 10), 0, 1000);
    } else {
        win_fl_state = BCM_WIN_STOPPED;
    }
    hal_gpio_write(BCM_PIN_WIN_FL_UP, win_fl_state == BCM_WIN_CLOSING);
    hal_gpio_write(BCM_PIN_WIN_FL_DN, win_fl_state == BCM_WIN_OPENING || win_fl_state == BCM_WIN_REVERSING);
    return win_fl_state;
}

int16_t bcm_window_fl_position(void)
{
    return win_fl_pos;
}

void bcm_window_fl_set_position(int16_t pos)
{
    win_fl_pos = clamp_i16(pos, 0, 1000);
}

void bcm_window_fl_reset(void)
{
    win_fl_pos = 0;
    win_fl_state = BCM_WIN_STOPPED;
    win_fl_reverse_ms = 0u;
    win_fl_pinches = 0u;
}


/* --------------------------------------------------------------------------------- window fr */
#define WIN_FR_CURRENT_LIMIT_MA 9000
static int16_t win_fr_pos;          /* 0 closed .. 1000 open, tenths of a percent */
static bcm_win_state_t win_fr_state;
static uint16_t win_fr_reverse_ms;
static uint8_t win_fr_pinches;

bcm_win_state_t bcm_window_fr(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms)
{
    uint16_t current = hal_adc_read(BCM_ADC_WIN_FR);
    bool pinch = win_fr_state == BCM_WIN_CLOSING && current > WIN_FR_CURRENT_LIMIT_MA && win_fr_pos > BCM_WIN_PINCH_ZONE;
    if (win_fr_state == BCM_WIN_REVERSING) {
        win_fr_reverse_ms = (uint16_t)(win_fr_reverse_ms + dt_ms);
        win_fr_pos = clamp_i16((int16_t)(win_fr_pos + 20), 0, 1000);
        if (win_fr_reverse_ms >= BCM_WIN_REVERSE_MS || win_fr_pos >= 1000) {
            win_fr_state = BCM_WIN_STOPPED;
            win_fr_reverse_ms = 0u;
        }
    } else if (pinch) {
        win_fr_state = BCM_WIN_REVERSING;
        win_fr_reverse_ms = 0u;
        win_fr_pinches++;
        if (win_fr_pinches >= BCM_WIN_MAX_PINCHES) {
            bcm_dtc_report(BCM_DTC_WINDOW_PINCH, true);
        }
    } else if (cmd == BCM_WIN_UP && win_fr_pos > 0) {
        win_fr_state = BCM_WIN_CLOSING;
        win_fr_pos = clamp_i16((int16_t)(win_fr_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_DOWN && win_fr_pos < 1000) {
        win_fr_state = BCM_WIN_OPENING;
        win_fr_pos = clamp_i16((int16_t)(win_fr_pos + 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_fr_state == BCM_WIN_CLOSING && win_fr_pos > 0) {
        win_fr_pos = clamp_i16((int16_t)(win_fr_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_fr_state == BCM_WIN_OPENING && win_fr_pos < 1000) {
        win_fr_pos = clamp_i16((int16_t)(win_fr_pos + 10), 0, 1000);
    } else {
        win_fr_state = BCM_WIN_STOPPED;
    }
    hal_gpio_write(BCM_PIN_WIN_FR_UP, win_fr_state == BCM_WIN_CLOSING);
    hal_gpio_write(BCM_PIN_WIN_FR_DN, win_fr_state == BCM_WIN_OPENING || win_fr_state == BCM_WIN_REVERSING);
    return win_fr_state;
}

int16_t bcm_window_fr_position(void)
{
    return win_fr_pos;
}

void bcm_window_fr_set_position(int16_t pos)
{
    win_fr_pos = clamp_i16(pos, 0, 1000);
}

void bcm_window_fr_reset(void)
{
    win_fr_pos = 0;
    win_fr_state = BCM_WIN_STOPPED;
    win_fr_reverse_ms = 0u;
    win_fr_pinches = 0u;
}


/* --------------------------------------------------------------------------------- window rl */
#define WIN_RL_CURRENT_LIMIT_MA 8000
static int16_t win_rl_pos;          /* 0 closed .. 1000 open, tenths of a percent */
static bcm_win_state_t win_rl_state;
static uint16_t win_rl_reverse_ms;
static uint8_t win_rl_pinches;

bcm_win_state_t bcm_window_rl(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms)
{
    uint16_t current = hal_adc_read(BCM_ADC_WIN_RL);
    bool pinch = win_rl_state == BCM_WIN_CLOSING && current > WIN_RL_CURRENT_LIMIT_MA && win_rl_pos > BCM_WIN_PINCH_ZONE;
    if (win_rl_state == BCM_WIN_REVERSING) {
        win_rl_reverse_ms = (uint16_t)(win_rl_reverse_ms + dt_ms);
        win_rl_pos = clamp_i16((int16_t)(win_rl_pos + 20), 0, 1000);
        if (win_rl_reverse_ms >= BCM_WIN_REVERSE_MS || win_rl_pos >= 1000) {
            win_rl_state = BCM_WIN_STOPPED;
            win_rl_reverse_ms = 0u;
        }
    } else if (pinch) {
        win_rl_state = BCM_WIN_REVERSING;
        win_rl_reverse_ms = 0u;
        win_rl_pinches++;
        if (win_rl_pinches >= BCM_WIN_MAX_PINCHES) {
            bcm_dtc_report(BCM_DTC_WINDOW_PINCH, true);
        }
    } else if (cmd == BCM_WIN_UP && win_rl_pos > 0) {
        win_rl_state = BCM_WIN_CLOSING;
        win_rl_pos = clamp_i16((int16_t)(win_rl_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_DOWN && win_rl_pos < 1000) {
        win_rl_state = BCM_WIN_OPENING;
        win_rl_pos = clamp_i16((int16_t)(win_rl_pos + 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_rl_state == BCM_WIN_CLOSING && win_rl_pos > 0) {
        win_rl_pos = clamp_i16((int16_t)(win_rl_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_rl_state == BCM_WIN_OPENING && win_rl_pos < 1000) {
        win_rl_pos = clamp_i16((int16_t)(win_rl_pos + 10), 0, 1000);
    } else {
        win_rl_state = BCM_WIN_STOPPED;
    }
    hal_gpio_write(BCM_PIN_WIN_RL_UP, win_rl_state == BCM_WIN_CLOSING);
    hal_gpio_write(BCM_PIN_WIN_RL_DN, win_rl_state == BCM_WIN_OPENING || win_rl_state == BCM_WIN_REVERSING);
    return win_rl_state;
}

int16_t bcm_window_rl_position(void)
{
    return win_rl_pos;
}

void bcm_window_rl_set_position(int16_t pos)
{
    win_rl_pos = clamp_i16(pos, 0, 1000);
}

void bcm_window_rl_reset(void)
{
    win_rl_pos = 0;
    win_rl_state = BCM_WIN_STOPPED;
    win_rl_reverse_ms = 0u;
    win_rl_pinches = 0u;
}


/* --------------------------------------------------------------------------------- window rr */
#define WIN_RR_CURRENT_LIMIT_MA 8000
static int16_t win_rr_pos;          /* 0 closed .. 1000 open, tenths of a percent */
static bcm_win_state_t win_rr_state;
static uint16_t win_rr_reverse_ms;
static uint8_t win_rr_pinches;

bcm_win_state_t bcm_window_rr(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms)
{
    uint16_t current = hal_adc_read(BCM_ADC_WIN_RR);
    bool pinch = win_rr_state == BCM_WIN_CLOSING && current > WIN_RR_CURRENT_LIMIT_MA && win_rr_pos > BCM_WIN_PINCH_ZONE;
    if (win_rr_state == BCM_WIN_REVERSING) {
        win_rr_reverse_ms = (uint16_t)(win_rr_reverse_ms + dt_ms);
        win_rr_pos = clamp_i16((int16_t)(win_rr_pos + 20), 0, 1000);
        if (win_rr_reverse_ms >= BCM_WIN_REVERSE_MS || win_rr_pos >= 1000) {
            win_rr_state = BCM_WIN_STOPPED;
            win_rr_reverse_ms = 0u;
        }
    } else if (pinch) {
        win_rr_state = BCM_WIN_REVERSING;
        win_rr_reverse_ms = 0u;
        win_rr_pinches++;
        if (win_rr_pinches >= BCM_WIN_MAX_PINCHES) {
            bcm_dtc_report(BCM_DTC_WINDOW_PINCH, true);
        }
    } else if (cmd == BCM_WIN_UP && win_rr_pos > 0) {
        win_rr_state = BCM_WIN_CLOSING;
        win_rr_pos = clamp_i16((int16_t)(win_rr_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_DOWN && win_rr_pos < 1000) {
        win_rr_state = BCM_WIN_OPENING;
        win_rr_pos = clamp_i16((int16_t)(win_rr_pos + 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_rr_state == BCM_WIN_CLOSING && win_rr_pos > 0) {
        win_rr_pos = clamp_i16((int16_t)(win_rr_pos - 10), 0, 1000);
    } else if (cmd == BCM_WIN_HOLD && express && win_rr_state == BCM_WIN_OPENING && win_rr_pos < 1000) {
        win_rr_pos = clamp_i16((int16_t)(win_rr_pos + 10), 0, 1000);
    } else {
        win_rr_state = BCM_WIN_STOPPED;
    }
    hal_gpio_write(BCM_PIN_WIN_RR_UP, win_rr_state == BCM_WIN_CLOSING);
    hal_gpio_write(BCM_PIN_WIN_RR_DN, win_rr_state == BCM_WIN_OPENING || win_rr_state == BCM_WIN_REVERSING);
    return win_rr_state;
}

int16_t bcm_window_rr_position(void)
{
    return win_rr_pos;
}

void bcm_window_rr_set_position(int16_t pos)
{
    win_rr_pos = clamp_i16(pos, 0, 1000);
}

void bcm_window_rr_reset(void)
{
    win_rr_pos = 0;
    win_rr_state = BCM_WIN_STOPPED;
    win_rr_reverse_ms = 0u;
    win_rr_pinches = 0u;
}


/* ----------------------------------------------------------------------------- driver seat heater */
static uint8_t seat_driver_level;
static uint32_t seat_driver_on_ms;

uint8_t bcm_seat_heater_driver(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms)
{
    int16_t temp = (int16_t)hal_adc_read(BCM_ADC_SEAT_DRV);
    int16_t target;
    int16_t error;
    uint8_t duty;
    if (power != BCM_POWER_RUN || !occupied || request > 3u) {
        seat_driver_level = 0u;
        seat_driver_on_ms = 0u;
        hal_pwm_set(BCM_PWM_SEAT_DRV, 0u);
        return 0u;
    }
    if (request != seat_driver_level) {
        seat_driver_level = request;
        seat_driver_on_ms = 0u;
    }
    seat_driver_on_ms += dt_ms;
    if (seat_driver_level == 3u && seat_driver_on_ms > BCM_SEAT_BOOST_MS) {
        seat_driver_level = 2u;
    } else if (seat_driver_level > 0u && seat_driver_on_ms > BCM_SEAT_MAX_MS) {
        seat_driver_level = 0u;
    }
    target = lut_seat_heat_target_lookup((int16_t)seat_driver_level);
    error = (int16_t)(target - temp);
    if (seat_driver_level == 0u || error <= 0) {
        duty = 0u;
    } else if (error > 100) {
        duty = 100u;
    } else {
        duty = (uint8_t)(error);
    }
    if (temp > BCM_SEAT_OVERTEMP) {
        duty = 0u;
        bcm_dtc_report(BCM_DTC_SEAT_OVERTEMP, true);
    }
    hal_pwm_set(BCM_PWM_SEAT_DRV, duty);
    return duty;
}

uint8_t bcm_seat_heater_driver_level(void)
{
    return seat_driver_level;
}

void bcm_seat_heater_driver_reset(void)
{
    seat_driver_level = 0u;
    seat_driver_on_ms = 0u;
}


/* ----------------------------------------------------------------------------- passenger seat heater */
static uint8_t seat_passenger_level;
static uint32_t seat_passenger_on_ms;

uint8_t bcm_seat_heater_passenger(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms)
{
    int16_t temp = (int16_t)hal_adc_read(BCM_ADC_SEAT_PAS);
    int16_t target;
    int16_t error;
    uint8_t duty;
    if (power != BCM_POWER_RUN || !occupied || request > 3u) {
        seat_passenger_level = 0u;
        seat_passenger_on_ms = 0u;
        hal_pwm_set(BCM_PWM_SEAT_PAS, 0u);
        return 0u;
    }
    if (request != seat_passenger_level) {
        seat_passenger_level = request;
        seat_passenger_on_ms = 0u;
    }
    seat_passenger_on_ms += dt_ms;
    if (seat_passenger_level == 3u && seat_passenger_on_ms > BCM_SEAT_BOOST_MS) {
        seat_passenger_level = 2u;
    } else if (seat_passenger_level > 0u && seat_passenger_on_ms > BCM_SEAT_MAX_MS) {
        seat_passenger_level = 0u;
    }
    target = lut_seat_heat_target_lookup((int16_t)seat_passenger_level);
    error = (int16_t)(target - temp);
    if (seat_passenger_level == 0u || error <= 0) {
        duty = 0u;
    } else if (error > 100) {
        duty = 100u;
    } else {
        duty = (uint8_t)(error);
    }
    if (temp > BCM_SEAT_OVERTEMP) {
        duty = 0u;
        bcm_dtc_report(BCM_DTC_SEAT_OVERTEMP, true);
    }
    hal_pwm_set(BCM_PWM_SEAT_PAS, duty);
    return duty;
}

uint8_t bcm_seat_heater_passenger_level(void)
{
    return seat_passenger_level;
}

void bcm_seat_heater_passenger_reset(void)
{
    seat_passenger_level = 0u;
    seat_passenger_on_ms = 0u;
}


/* ----------------------------------------------------------------------------- rear_left seat heater */
static uint8_t seat_rear_left_level;
static uint32_t seat_rear_left_on_ms;

uint8_t bcm_seat_heater_rear_left(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms)
{
    int16_t temp = (int16_t)hal_adc_read(BCM_ADC_SEAT_RL);
    int16_t target;
    int16_t error;
    uint8_t duty;
    if (power != BCM_POWER_RUN || !occupied || request > 3u) {
        seat_rear_left_level = 0u;
        seat_rear_left_on_ms = 0u;
        hal_pwm_set(BCM_PWM_SEAT_RL, 0u);
        return 0u;
    }
    if (request != seat_rear_left_level) {
        seat_rear_left_level = request;
        seat_rear_left_on_ms = 0u;
    }
    seat_rear_left_on_ms += dt_ms;
    if (seat_rear_left_level == 3u && seat_rear_left_on_ms > BCM_SEAT_BOOST_MS) {
        seat_rear_left_level = 2u;
    } else if (seat_rear_left_level > 0u && seat_rear_left_on_ms > BCM_SEAT_MAX_MS) {
        seat_rear_left_level = 0u;
    }
    target = lut_seat_heat_target_lookup((int16_t)seat_rear_left_level);
    error = (int16_t)(target - temp);
    if (seat_rear_left_level == 0u || error <= 0) {
        duty = 0u;
    } else if (error > 100) {
        duty = 100u;
    } else {
        duty = (uint8_t)(error);
    }
    if (temp > BCM_SEAT_OVERTEMP) {
        duty = 0u;
        bcm_dtc_report(BCM_DTC_SEAT_OVERTEMP, true);
    }
    hal_pwm_set(BCM_PWM_SEAT_RL, duty);
    return duty;
}

uint8_t bcm_seat_heater_rear_left_level(void)
{
    return seat_rear_left_level;
}

void bcm_seat_heater_rear_left_reset(void)
{
    seat_rear_left_level = 0u;
    seat_rear_left_on_ms = 0u;
}


/* ----------------------------------------------------------------------------- rear_right seat heater */
static uint8_t seat_rear_right_level;
static uint32_t seat_rear_right_on_ms;

uint8_t bcm_seat_heater_rear_right(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms)
{
    int16_t temp = (int16_t)hal_adc_read(BCM_ADC_SEAT_RR);
    int16_t target;
    int16_t error;
    uint8_t duty;
    if (power != BCM_POWER_RUN || !occupied || request > 3u) {
        seat_rear_right_level = 0u;
        seat_rear_right_on_ms = 0u;
        hal_pwm_set(BCM_PWM_SEAT_RR, 0u);
        return 0u;
    }
    if (request != seat_rear_right_level) {
        seat_rear_right_level = request;
        seat_rear_right_on_ms = 0u;
    }
    seat_rear_right_on_ms += dt_ms;
    if (seat_rear_right_level == 3u && seat_rear_right_on_ms > BCM_SEAT_BOOST_MS) {
        seat_rear_right_level = 2u;
    } else if (seat_rear_right_level > 0u && seat_rear_right_on_ms > BCM_SEAT_MAX_MS) {
        seat_rear_right_level = 0u;
    }
    target = lut_seat_heat_target_lookup((int16_t)seat_rear_right_level);
    error = (int16_t)(target - temp);
    if (seat_rear_right_level == 0u || error <= 0) {
        duty = 0u;
    } else if (error > 100) {
        duty = 100u;
    } else {
        duty = (uint8_t)(error);
    }
    if (temp > BCM_SEAT_OVERTEMP) {
        duty = 0u;
        bcm_dtc_report(BCM_DTC_SEAT_OVERTEMP, true);
    }
    hal_pwm_set(BCM_PWM_SEAT_RR, duty);
    return duty;
}

uint8_t bcm_seat_heater_rear_right_level(void)
{
    return seat_rear_right_level;
}

void bcm_seat_heater_rear_right_reset(void)
{
    seat_rear_right_level = 0u;
    seat_rear_right_on_ms = 0u;
}


/* ------------------------------------------------------------------------------ battery and load shed */
static uint8_t g_shed_level;

uint8_t bcm_load_shed(int32_t battery_mv, bool engine_running)
{
    int32_t mv = ema_battery_mv_update(battery_mv);
    if (engine_running) {
        g_shed_level = 0u;
    } else if (mv < BCM_SHED3_MV) {
        g_shed_level = 3u;
    } else if (mv < BCM_SHED2_MV && g_shed_level < 2u) {
        g_shed_level = 2u;
    } else if (mv < BCM_SHED1_MV && g_shed_level < 1u) {
        g_shed_level = 1u;
    } else if (mv > BCM_SHED1_MV + BCM_SHED_HYST_MV) {
        g_shed_level = 0u;
    } else if (mv > BCM_SHED2_MV + BCM_SHED_HYST_MV && g_shed_level > 1u) {
        g_shed_level = 1u;
    }
    return g_shed_level;
}

bool bcm_load_allowed(bcm_load_t load, uint8_t shed_level)
{
    switch (load) {
    case BCM_LOAD_COMFORT:
        return shed_level == 0u;
    case BCM_LOAD_HEATING:
        return shed_level <= 1u;
    case BCM_LOAD_LIGHTING:
        return shed_level <= 2u;
    case BCM_LOAD_SAFETY:
        return true;
    default:
        return false;
    }
}

void bcm_load_shed_reset(void)
{
    g_shed_level = 0u;
    ema_battery_mv_reset();
}


/* ----------------------------------------------------------------------------------- interior light */
static uint16_t g_courtesy_ms;
static uint8_t g_dim;

uint8_t bcm_interior_light(bool any_door_open, bool switch_on, bool locked, uint16_t dt_ms)
{
    uint8_t target;
    if (switch_on || any_door_open) {
        g_courtesy_ms = BCM_COURTESY_MS;
        target = 100u;
    } else if (locked) {
        g_courtesy_ms = 0u;
        target = 0u;
    } else if (g_courtesy_ms > dt_ms) {
        g_courtesy_ms = (uint16_t)(g_courtesy_ms - dt_ms);
        target = 100u;
    } else {
        g_courtesy_ms = 0u;
        target = 0u;
    }
    if (g_dim < target) {
        g_dim = (uint8_t)(g_dim + ((uint8_t)(target - g_dim) > 10u ? 10u : (uint8_t)(target - g_dim)));
    } else if (g_dim > target) {
        g_dim = (uint8_t)(g_dim - ((uint8_t)(g_dim - target) > 5u ? 5u : (uint8_t)(g_dim - target)));
    }
    hal_pwm_set(BCM_PWM_INTERIOR, (uint8_t)lut_dim_curve_lookup((int16_t)g_dim));
    return g_dim;
}

void bcm_interior_light_reset(void)
{
    g_courtesy_ms = 0u;
    g_dim = 0u;
}

/* ---------------------------------------------------------------------------------------------- horn */
static uint16_t g_horn_ms;
static uint8_t g_horn_chirps;

bool bcm_horn(bool pressed, bcm_horn_pattern_t pattern, uint16_t dt_ms)
{
    bool on = false;
    if (pressed) {
        g_horn_chirps = 0u;
        on = true;
    } else if (pattern == BCM_HORN_LOCK_CHIRP && g_horn_chirps == 0u && g_horn_ms == 0u) {
        g_horn_chirps = 1u;
        g_horn_ms = BCM_CHIRP_MS;
        on = true;
    } else if (pattern == BCM_HORN_ALARM) {
        g_horn_ms = (uint16_t)((g_horn_ms + dt_ms) % (2u * BCM_ALARM_HALF_MS));
        on = g_horn_ms < BCM_ALARM_HALF_MS;
    } else if (g_horn_ms > dt_ms) {
        g_horn_ms = (uint16_t)(g_horn_ms - dt_ms);
        on = g_horn_chirps > 0u;
    } else {
        g_horn_ms = 0u;
        g_horn_chirps = 0u;
    }
    hal_gpio_write(BCM_PIN_HORN, on);
    return on;
}

void bcm_horn_reset(void)
{
    g_horn_ms = 0u;
    g_horn_chirps = 0u;
}

/* -------------------------------------------------------------------------------------------- mirrors */
static bool g_mirror_folded;
static bool g_mirror_heat;
static uint32_t g_mirror_heat_ms;

bool bcm_mirror_fold(bool fold_request, bool locked, int16_t speed_kph, bool auto_fold)
{
    if (speed_kph > BCM_MIRROR_MAX_KPH) {
        g_mirror_folded = false;
    } else if (fold_request) {
        g_mirror_folded = !g_mirror_folded;
    } else if (auto_fold && locked) {
        g_mirror_folded = true;
    } else if (auto_fold && !locked && speed_kph > 0) {
        g_mirror_folded = false;
    }
    hal_gpio_write(BCM_PIN_MIRROR_FOLD, g_mirror_folded);
    return g_mirror_folded;
}

bool bcm_mirror_heat(bool rear_defrost, int16_t outside_temp, uint16_t dt_ms)
{
    if (rear_defrost && !g_mirror_heat) {
        g_mirror_heat = true;
        g_mirror_heat_ms = 0u;
    } else if (g_mirror_heat) {
        g_mirror_heat_ms += dt_ms;
        if (g_mirror_heat_ms >= BCM_MIRROR_HEAT_MS && outside_temp > BCM_MIRROR_COLD) {
            g_mirror_heat = false;
        }
    }
    hal_gpio_write(BCM_PIN_MIRROR_HEAT, g_mirror_heat);
    return g_mirror_heat;
}

void bcm_mirror_reset(void)
{
    g_mirror_folded = false;
    g_mirror_heat = false;
    g_mirror_heat_ms = 0u;
}


/* ----------------------------------------------------------------------------------------- CAN layer */
uint8_t bcm_crc8_j1850(const uint8_t *data, uint8_t len)
{
    uint8_t crc = 0xFFu;
    uint8_t i;
    uint8_t bit;
    if (data == NULL) {
        return 0u;
    }
    for (i = 0u; i < len; i++) {
        crc ^= data[i];
        for (bit = 0u; bit < 8u; bit++) {
            if ((crc & 0x80u) != 0u) {
                crc = (uint8_t)((crc << 1) ^ 0x1Du);
            } else {
                crc = (uint8_t)(crc << 1);
            }
        }
    }
    return (uint8_t)(crc ^ 0xFFu);
}

uint32_t bcm_get_bits(const uint8_t *data, uint8_t start, uint8_t length)
{
    uint32_t value = 0u;
    uint8_t i;
    if (data == NULL || length == 0u || length > 32u || start + length > 64u) {
        return 0u;
    }
    for (i = 0u; i < length; i++) {
        uint8_t bit = (uint8_t)(start + i);
        if ((data[bit / 8u] & (1u << (bit % 8u))) != 0u) {
            value |= (1u << i);
        }
    }
    return value;
}

bool bcm_set_bits(uint8_t *data, uint8_t start, uint8_t length, uint32_t value)
{
    uint8_t i;
    if (data == NULL || length == 0u || length > 32u || start + length > 64u) {
        return false;
    }
    if (length < 32u && value >= (1u << length)) {
        return false;
    }
    for (i = 0u; i < length; i++) {
        uint8_t bit = (uint8_t)(start + i);
        if ((value & (1u << i)) != 0u) {
            data[bit / 8u] = (uint8_t)(data[bit / 8u] | (1u << (bit % 8u)));
        } else {
            data[bit / 8u] = (uint8_t)(data[bit / 8u] & ~(1u << (bit % 8u)));
        }
    }
    return true;
}

#define DEFINE_SIGNAL(name, start, len, factor, offset, lo, hi)                          \
    int32_t name##_decode(const uint8_t *data)                                            \
    {                                                                                     \
        int32_t raw = (int32_t)bcm_get_bits(data, (start), (len));                        \
        return raw * (factor) + (offset);                                                 \
    }                                                                                     \
    bool name##_encode(uint8_t *data, int32_t physical)                                   \
    {                                                                                     \
        int32_t raw;                                                                      \
        if (physical < (lo) || physical > (hi)) {                                         \
            return false;                                                                 \
        }                                                                                 \
        raw = (physical - (offset)) / (factor);                                           \
        return bcm_set_bits(data, (start), (len), (uint32_t)raw);                         \
    }

DEFINE_SIGNAL(sig_vehicle_speed, 0u, 13u, 1, 0, 0, 320)

DEFINE_SIGNAL(sig_outside_temp, 16u, 8u, 1, -40, -40, 85)

DEFINE_SIGNAL(sig_light_lux, 24u, 16u, 2, 0, 0, 100000)

DEFINE_SIGNAL(sig_rain_level, 40u, 4u, 1, 0, 0, 15)

DEFINE_SIGNAL(sig_battery_mv, 44u, 12u, 5, 6000, 6000, 18000)

DEFINE_SIGNAL(sig_body_pitch, 56u, 8u, 1, -128, -128, 127)

DEFINE_SIGNAL(sig_door_state, 0u, 4u, 1, 0, 0, 15)

DEFINE_SIGNAL(sig_lock_req, 4u, 2u, 1, 0, 0, 3)

DEFINE_SIGNAL(sig_key_pos, 6u, 2u, 1, 0, 0, 3)

DEFINE_SIGNAL(sig_crash, 8u, 1u, 1, 0, 0, 1)


/* ------------------------------------------------------------------------------ msg_vehicle reception */
static uint8_t msg_vehicle_alive;
static uint16_t msg_vehicle_age_ms;
static uint8_t msg_vehicle_errors;
static bool msg_vehicle_valid;

bool bcm_msg_vehicle_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_vehicle_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_vehicle_errors++;
        if (msg_vehicle_errors >= 3u) {
            msg_vehicle_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_vehicle_alive + 1u) & 0x0Fu);
    if (msg_vehicle_valid && counter != expected && counter != msg_vehicle_alive) {
        msg_vehicle_errors++;
    } else if (msg_vehicle_errors > 0u) {
        msg_vehicle_errors--;
    }
    msg_vehicle_alive = counter;
    msg_vehicle_age_ms = 0u;
    msg_vehicle_valid = msg_vehicle_errors < 3u;
    return msg_vehicle_valid;
}

bool bcm_msg_vehicle_tick(uint16_t dt_ms)
{
    if (msg_vehicle_age_ms < 3u * 100u) {
        msg_vehicle_age_ms = (uint16_t)(msg_vehicle_age_ms + dt_ms);
    }
    if (msg_vehicle_age_ms >= 3u * 100u) {
        msg_vehicle_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_vehicle_valid;
}

uint32_t bcm_msg_vehicle_id(void)
{
    return 0x120u;
}

void bcm_msg_vehicle_reset(void)
{
    msg_vehicle_alive = 0u;
    msg_vehicle_age_ms = 0u;
    msg_vehicle_errors = 0u;
    msg_vehicle_valid = false;
}


/* ------------------------------------------------------------------------------ msg_engine reception */
static uint8_t msg_engine_alive;
static uint16_t msg_engine_age_ms;
static uint8_t msg_engine_errors;
static bool msg_engine_valid;

bool bcm_msg_engine_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_engine_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_engine_errors++;
        if (msg_engine_errors >= 5u) {
            msg_engine_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_engine_alive + 1u) & 0x0Fu);
    if (msg_engine_valid && counter != expected && counter != msg_engine_alive) {
        msg_engine_errors++;
    } else if (msg_engine_errors > 0u) {
        msg_engine_errors--;
    }
    msg_engine_alive = counter;
    msg_engine_age_ms = 0u;
    msg_engine_valid = msg_engine_errors < 5u;
    return msg_engine_valid;
}

bool bcm_msg_engine_tick(uint16_t dt_ms)
{
    if (msg_engine_age_ms < 3u * 20u) {
        msg_engine_age_ms = (uint16_t)(msg_engine_age_ms + dt_ms);
    }
    if (msg_engine_age_ms >= 3u * 20u) {
        msg_engine_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_engine_valid;
}

uint32_t bcm_msg_engine_id(void)
{
    return 0x130u;
}

void bcm_msg_engine_reset(void)
{
    msg_engine_alive = 0u;
    msg_engine_age_ms = 0u;
    msg_engine_errors = 0u;
    msg_engine_valid = false;
}


/* ------------------------------------------------------------------------------ msg_brake reception */
static uint8_t msg_brake_alive;
static uint16_t msg_brake_age_ms;
static uint8_t msg_brake_errors;
static bool msg_brake_valid;

bool bcm_msg_brake_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_brake_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_brake_errors++;
        if (msg_brake_errors >= 4u) {
            msg_brake_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_brake_alive + 1u) & 0x0Fu);
    if (msg_brake_valid && counter != expected && counter != msg_brake_alive) {
        msg_brake_errors++;
    } else if (msg_brake_errors > 0u) {
        msg_brake_errors--;
    }
    msg_brake_alive = counter;
    msg_brake_age_ms = 0u;
    msg_brake_valid = msg_brake_errors < 4u;
    return msg_brake_valid;
}

bool bcm_msg_brake_tick(uint16_t dt_ms)
{
    if (msg_brake_age_ms < 3u * 10u) {
        msg_brake_age_ms = (uint16_t)(msg_brake_age_ms + dt_ms);
    }
    if (msg_brake_age_ms >= 3u * 10u) {
        msg_brake_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_brake_valid;
}

uint32_t bcm_msg_brake_id(void)
{
    return 0x140u;
}

void bcm_msg_brake_reset(void)
{
    msg_brake_alive = 0u;
    msg_brake_age_ms = 0u;
    msg_brake_errors = 0u;
    msg_brake_valid = false;
}


/* ------------------------------------------------------------------------------ msg_climate reception */
static uint8_t msg_climate_alive;
static uint16_t msg_climate_age_ms;
static uint8_t msg_climate_errors;
static bool msg_climate_valid;

bool bcm_msg_climate_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_climate_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_climate_errors++;
        if (msg_climate_errors >= 2u) {
            msg_climate_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_climate_alive + 1u) & 0x0Fu);
    if (msg_climate_valid && counter != expected && counter != msg_climate_alive) {
        msg_climate_errors++;
    } else if (msg_climate_errors > 0u) {
        msg_climate_errors--;
    }
    msg_climate_alive = counter;
    msg_climate_age_ms = 0u;
    msg_climate_valid = msg_climate_errors < 2u;
    return msg_climate_valid;
}

bool bcm_msg_climate_tick(uint16_t dt_ms)
{
    if (msg_climate_age_ms < 3u * 200u) {
        msg_climate_age_ms = (uint16_t)(msg_climate_age_ms + dt_ms);
    }
    if (msg_climate_age_ms >= 3u * 200u) {
        msg_climate_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_climate_valid;
}

uint32_t bcm_msg_climate_id(void)
{
    return 0x2A0u;
}

void bcm_msg_climate_reset(void)
{
    msg_climate_alive = 0u;
    msg_climate_age_ms = 0u;
    msg_climate_errors = 0u;
    msg_climate_valid = false;
}


/* ------------------------------------------------------------------------------ msg_door reception */
static uint8_t msg_door_alive;
static uint16_t msg_door_age_ms;
static uint8_t msg_door_errors;
static bool msg_door_valid;

bool bcm_msg_door_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_door_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_door_errors++;
        if (msg_door_errors >= 3u) {
            msg_door_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_door_alive + 1u) & 0x0Fu);
    if (msg_door_valid && counter != expected && counter != msg_door_alive) {
        msg_door_errors++;
    } else if (msg_door_errors > 0u) {
        msg_door_errors--;
    }
    msg_door_alive = counter;
    msg_door_age_ms = 0u;
    msg_door_valid = msg_door_errors < 3u;
    return msg_door_valid;
}

bool bcm_msg_door_tick(uint16_t dt_ms)
{
    if (msg_door_age_ms < 3u * 50u) {
        msg_door_age_ms = (uint16_t)(msg_door_age_ms + dt_ms);
    }
    if (msg_door_age_ms >= 3u * 50u) {
        msg_door_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_door_valid;
}

uint32_t bcm_msg_door_id(void)
{
    return 0x310u;
}

void bcm_msg_door_reset(void)
{
    msg_door_alive = 0u;
    msg_door_age_ms = 0u;
    msg_door_errors = 0u;
    msg_door_valid = false;
}


/* ------------------------------------------------------------------------------ msg_airbag reception */
static uint8_t msg_airbag_alive;
static uint16_t msg_airbag_age_ms;
static uint8_t msg_airbag_errors;
static bool msg_airbag_valid;

bool bcm_msg_airbag_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_airbag_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_airbag_errors++;
        if (msg_airbag_errors >= 2u) {
            msg_airbag_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_airbag_alive + 1u) & 0x0Fu);
    if (msg_airbag_valid && counter != expected && counter != msg_airbag_alive) {
        msg_airbag_errors++;
    } else if (msg_airbag_errors > 0u) {
        msg_airbag_errors--;
    }
    msg_airbag_alive = counter;
    msg_airbag_age_ms = 0u;
    msg_airbag_valid = msg_airbag_errors < 2u;
    return msg_airbag_valid;
}

bool bcm_msg_airbag_tick(uint16_t dt_ms)
{
    if (msg_airbag_age_ms < 3u * 10u) {
        msg_airbag_age_ms = (uint16_t)(msg_airbag_age_ms + dt_ms);
    }
    if (msg_airbag_age_ms >= 3u * 10u) {
        msg_airbag_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_airbag_valid;
}

uint32_t bcm_msg_airbag_id(void)
{
    return 0x050u;
}

void bcm_msg_airbag_reset(void)
{
    msg_airbag_alive = 0u;
    msg_airbag_age_ms = 0u;
    msg_airbag_errors = 0u;
    msg_airbag_valid = false;
}


/* ------------------------------------------------------------------------------ msg_tpms reception */
static uint8_t msg_tpms_alive;
static uint16_t msg_tpms_age_ms;
static uint8_t msg_tpms_errors;
static bool msg_tpms_valid;

bool bcm_msg_tpms_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_tpms_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_tpms_errors++;
        if (msg_tpms_errors >= 2u) {
            msg_tpms_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_tpms_alive + 1u) & 0x0Fu);
    if (msg_tpms_valid && counter != expected && counter != msg_tpms_alive) {
        msg_tpms_errors++;
    } else if (msg_tpms_errors > 0u) {
        msg_tpms_errors--;
    }
    msg_tpms_alive = counter;
    msg_tpms_age_ms = 0u;
    msg_tpms_valid = msg_tpms_errors < 2u;
    return msg_tpms_valid;
}

bool bcm_msg_tpms_tick(uint16_t dt_ms)
{
    if (msg_tpms_age_ms < 3u * 500u) {
        msg_tpms_age_ms = (uint16_t)(msg_tpms_age_ms + dt_ms);
    }
    if (msg_tpms_age_ms >= 3u * 500u) {
        msg_tpms_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_tpms_valid;
}

uint32_t bcm_msg_tpms_id(void)
{
    return 0x385u;
}

void bcm_msg_tpms_reset(void)
{
    msg_tpms_alive = 0u;
    msg_tpms_age_ms = 0u;
    msg_tpms_errors = 0u;
    msg_tpms_valid = false;
}


/* ------------------------------------------------------------------------------ msg_park reception */
static uint8_t msg_park_alive;
static uint16_t msg_park_age_ms;
static uint8_t msg_park_errors;
static bool msg_park_valid;

bool bcm_msg_park_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_park_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_park_errors++;
        if (msg_park_errors >= 3u) {
            msg_park_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_park_alive + 1u) & 0x0Fu);
    if (msg_park_valid && counter != expected && counter != msg_park_alive) {
        msg_park_errors++;
    } else if (msg_park_errors > 0u) {
        msg_park_errors--;
    }
    msg_park_alive = counter;
    msg_park_age_ms = 0u;
    msg_park_valid = msg_park_errors < 3u;
    return msg_park_valid;
}

bool bcm_msg_park_tick(uint16_t dt_ms)
{
    if (msg_park_age_ms < 3u * 50u) {
        msg_park_age_ms = (uint16_t)(msg_park_age_ms + dt_ms);
    }
    if (msg_park_age_ms >= 3u * 50u) {
        msg_park_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_park_valid;
}

uint32_t bcm_msg_park_id(void)
{
    return 0x2F0u;
}

void bcm_msg_park_reset(void)
{
    msg_park_alive = 0u;
    msg_park_age_ms = 0u;
    msg_park_errors = 0u;
    msg_park_valid = false;
}


/* ------------------------------------------------------------------------------ msg_gateway reception */
static uint8_t msg_gateway_alive;
static uint16_t msg_gateway_age_ms;
static uint8_t msg_gateway_errors;
static bool msg_gateway_valid;

bool bcm_msg_gateway_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_gateway_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_gateway_errors++;
        if (msg_gateway_errors >= 5u) {
            msg_gateway_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_gateway_alive + 1u) & 0x0Fu);
    if (msg_gateway_valid && counter != expected && counter != msg_gateway_alive) {
        msg_gateway_errors++;
    } else if (msg_gateway_errors > 0u) {
        msg_gateway_errors--;
    }
    msg_gateway_alive = counter;
    msg_gateway_age_ms = 0u;
    msg_gateway_valid = msg_gateway_errors < 5u;
    return msg_gateway_valid;
}

bool bcm_msg_gateway_tick(uint16_t dt_ms)
{
    if (msg_gateway_age_ms < 3u * 20u) {
        msg_gateway_age_ms = (uint16_t)(msg_gateway_age_ms + dt_ms);
    }
    if (msg_gateway_age_ms >= 3u * 20u) {
        msg_gateway_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_gateway_valid;
}

uint32_t bcm_msg_gateway_id(void)
{
    return 0x100u;
}

void bcm_msg_gateway_reset(void)
{
    msg_gateway_alive = 0u;
    msg_gateway_age_ms = 0u;
    msg_gateway_errors = 0u;
    msg_gateway_valid = false;
}


/* ------------------------------------------------------------------------------ msg_steering reception */
static uint8_t msg_steering_alive;
static uint16_t msg_steering_age_ms;
static uint8_t msg_steering_errors;
static bool msg_steering_valid;

bool bcm_msg_steering_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_steering_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_steering_errors++;
        if (msg_steering_errors >= 4u) {
            msg_steering_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_steering_alive + 1u) & 0x0Fu);
    if (msg_steering_valid && counter != expected && counter != msg_steering_alive) {
        msg_steering_errors++;
    } else if (msg_steering_errors > 0u) {
        msg_steering_errors--;
    }
    msg_steering_alive = counter;
    msg_steering_age_ms = 0u;
    msg_steering_valid = msg_steering_errors < 4u;
    return msg_steering_valid;
}

bool bcm_msg_steering_tick(uint16_t dt_ms)
{
    if (msg_steering_age_ms < 3u * 10u) {
        msg_steering_age_ms = (uint16_t)(msg_steering_age_ms + dt_ms);
    }
    if (msg_steering_age_ms >= 3u * 10u) {
        msg_steering_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_steering_valid;
}

uint32_t bcm_msg_steering_id(void)
{
    return 0x0C0u;
}

void bcm_msg_steering_reset(void)
{
    msg_steering_alive = 0u;
    msg_steering_age_ms = 0u;
    msg_steering_errors = 0u;
    msg_steering_valid = false;
}


/* ------------------------------------------------------------------------------ msg_wheel_speed reception */
static uint8_t msg_wheel_speed_alive;
static uint16_t msg_wheel_speed_age_ms;
static uint8_t msg_wheel_speed_errors;
static bool msg_wheel_speed_valid;

bool bcm_msg_wheel_speed_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_wheel_speed_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_wheel_speed_errors++;
        if (msg_wheel_speed_errors >= 4u) {
            msg_wheel_speed_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_wheel_speed_alive + 1u) & 0x0Fu);
    if (msg_wheel_speed_valid && counter != expected && counter != msg_wheel_speed_alive) {
        msg_wheel_speed_errors++;
    } else if (msg_wheel_speed_errors > 0u) {
        msg_wheel_speed_errors--;
    }
    msg_wheel_speed_alive = counter;
    msg_wheel_speed_age_ms = 0u;
    msg_wheel_speed_valid = msg_wheel_speed_errors < 4u;
    return msg_wheel_speed_valid;
}

bool bcm_msg_wheel_speed_tick(uint16_t dt_ms)
{
    if (msg_wheel_speed_age_ms < 3u * 10u) {
        msg_wheel_speed_age_ms = (uint16_t)(msg_wheel_speed_age_ms + dt_ms);
    }
    if (msg_wheel_speed_age_ms >= 3u * 10u) {
        msg_wheel_speed_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_wheel_speed_valid;
}

uint32_t bcm_msg_wheel_speed_id(void)
{
    return 0x0B0u;
}

void bcm_msg_wheel_speed_reset(void)
{
    msg_wheel_speed_alive = 0u;
    msg_wheel_speed_age_ms = 0u;
    msg_wheel_speed_errors = 0u;
    msg_wheel_speed_valid = false;
}


/* ------------------------------------------------------------------------------ msg_transmission reception */
static uint8_t msg_transmission_alive;
static uint16_t msg_transmission_age_ms;
static uint8_t msg_transmission_errors;
static bool msg_transmission_valid;

bool bcm_msg_transmission_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_transmission_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_transmission_errors++;
        if (msg_transmission_errors >= 3u) {
            msg_transmission_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_transmission_alive + 1u) & 0x0Fu);
    if (msg_transmission_valid && counter != expected && counter != msg_transmission_alive) {
        msg_transmission_errors++;
    } else if (msg_transmission_errors > 0u) {
        msg_transmission_errors--;
    }
    msg_transmission_alive = counter;
    msg_transmission_age_ms = 0u;
    msg_transmission_valid = msg_transmission_errors < 3u;
    return msg_transmission_valid;
}

bool bcm_msg_transmission_tick(uint16_t dt_ms)
{
    if (msg_transmission_age_ms < 3u * 20u) {
        msg_transmission_age_ms = (uint16_t)(msg_transmission_age_ms + dt_ms);
    }
    if (msg_transmission_age_ms >= 3u * 20u) {
        msg_transmission_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_transmission_valid;
}

uint32_t bcm_msg_transmission_id(void)
{
    return 0x1A0u;
}

void bcm_msg_transmission_reset(void)
{
    msg_transmission_alive = 0u;
    msg_transmission_age_ms = 0u;
    msg_transmission_errors = 0u;
    msg_transmission_valid = false;
}


/* ------------------------------------------------------------------------------ msg_infotainment reception */
static uint8_t msg_infotainment_alive;
static uint16_t msg_infotainment_age_ms;
static uint8_t msg_infotainment_errors;
static bool msg_infotainment_valid;

bool bcm_msg_infotainment_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_infotainment_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_infotainment_errors++;
        if (msg_infotainment_errors >= 2u) {
            msg_infotainment_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_infotainment_alive + 1u) & 0x0Fu);
    if (msg_infotainment_valid && counter != expected && counter != msg_infotainment_alive) {
        msg_infotainment_errors++;
    } else if (msg_infotainment_errors > 0u) {
        msg_infotainment_errors--;
    }
    msg_infotainment_alive = counter;
    msg_infotainment_age_ms = 0u;
    msg_infotainment_valid = msg_infotainment_errors < 2u;
    return msg_infotainment_valid;
}

bool bcm_msg_infotainment_tick(uint16_t dt_ms)
{
    if (msg_infotainment_age_ms < 3u * 200u) {
        msg_infotainment_age_ms = (uint16_t)(msg_infotainment_age_ms + dt_ms);
    }
    if (msg_infotainment_age_ms >= 3u * 200u) {
        msg_infotainment_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_infotainment_valid;
}

uint32_t bcm_msg_infotainment_id(void)
{
    return 0x3E0u;
}

void bcm_msg_infotainment_reset(void)
{
    msg_infotainment_alive = 0u;
    msg_infotainment_age_ms = 0u;
    msg_infotainment_errors = 0u;
    msg_infotainment_valid = false;
}


/* ------------------------------------------------------------------------------ msg_adas reception */
static uint8_t msg_adas_alive;
static uint16_t msg_adas_age_ms;
static uint8_t msg_adas_errors;
static bool msg_adas_valid;

bool bcm_msg_adas_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_adas_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_adas_errors++;
        if (msg_adas_errors >= 4u) {
            msg_adas_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_adas_alive + 1u) & 0x0Fu);
    if (msg_adas_valid && counter != expected && counter != msg_adas_alive) {
        msg_adas_errors++;
    } else if (msg_adas_errors > 0u) {
        msg_adas_errors--;
    }
    msg_adas_alive = counter;
    msg_adas_age_ms = 0u;
    msg_adas_valid = msg_adas_errors < 4u;
    return msg_adas_valid;
}

bool bcm_msg_adas_tick(uint16_t dt_ms)
{
    if (msg_adas_age_ms < 3u * 20u) {
        msg_adas_age_ms = (uint16_t)(msg_adas_age_ms + dt_ms);
    }
    if (msg_adas_age_ms >= 3u * 20u) {
        msg_adas_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_adas_valid;
}

uint32_t bcm_msg_adas_id(void)
{
    return 0x0F0u;
}

void bcm_msg_adas_reset(void)
{
    msg_adas_alive = 0u;
    msg_adas_age_ms = 0u;
    msg_adas_errors = 0u;
    msg_adas_valid = false;
}


/* ------------------------------------------------------------------------------ msg_lighting_req reception */
static uint8_t msg_lighting_req_alive;
static uint16_t msg_lighting_req_age_ms;
static uint8_t msg_lighting_req_errors;
static bool msg_lighting_req_valid;

bool bcm_msg_lighting_req_receive(const uint8_t *data, uint8_t len)
{
    uint8_t counter;
    uint8_t expected;
    if (data == NULL || len != 8u) {
        msg_lighting_req_errors++;
        return false;
    }
    if (bcm_crc8_j1850(&data[1], 7u) != data[0]) {
        msg_lighting_req_errors++;
        if (msg_lighting_req_errors >= 3u) {
            msg_lighting_req_valid = false;
            bcm_dtc_report(BCM_DTC_CAN_CRC, true);
        }
        return false;
    }
    counter = (uint8_t)(data[1] & 0x0Fu);
    expected = (uint8_t)((msg_lighting_req_alive + 1u) & 0x0Fu);
    if (msg_lighting_req_valid && counter != expected && counter != msg_lighting_req_alive) {
        msg_lighting_req_errors++;
    } else if (msg_lighting_req_errors > 0u) {
        msg_lighting_req_errors--;
    }
    msg_lighting_req_alive = counter;
    msg_lighting_req_age_ms = 0u;
    msg_lighting_req_valid = msg_lighting_req_errors < 3u;
    return msg_lighting_req_valid;
}

bool bcm_msg_lighting_req_tick(uint16_t dt_ms)
{
    if (msg_lighting_req_age_ms < 3u * 100u) {
        msg_lighting_req_age_ms = (uint16_t)(msg_lighting_req_age_ms + dt_ms);
    }
    if (msg_lighting_req_age_ms >= 3u * 100u) {
        msg_lighting_req_valid = false;
        bcm_dtc_report(BCM_DTC_CAN_TIMEOUT, true);
    }
    return msg_lighting_req_valid;
}

uint32_t bcm_msg_lighting_req_id(void)
{
    return 0x330u;
}

void bcm_msg_lighting_req_reset(void)
{
    msg_lighting_req_alive = 0u;
    msg_lighting_req_age_ms = 0u;
    msg_lighting_req_errors = 0u;
    msg_lighting_req_valid = false;
}


/* ---------------------------------------------------------------------------- diagnostic trouble codes */
typedef struct {
    int8_t counter;          /* -BCM_DTC_PASS_LIMIT .. +BCM_DTC_FAIL_LIMIT */
    uint8_t status;          /* bit 0 test failed, bit 3 confirmed, bit 6 not completed this cycle */
    uint8_t aging;
    uint8_t occurrences;
} bcm_dtc_entry_t;

static bcm_dtc_entry_t g_dtc[BCM_DTC_COUNT];
static bool g_dtc_frozen;

#define DTC_FAILED 0x01u
#define DTC_CONFIRMED 0x08u
#define DTC_NOT_COMPLETE 0x40u

void bcm_dtc_report(bcm_dtc_t id, bool failed)
{
    bcm_dtc_entry_t *e;
    if ((unsigned)id >= (unsigned)BCM_DTC_COUNT || g_dtc_frozen) {
        return;
    }
    e = &g_dtc[id];
    e->status = (uint8_t)(e->status & ~DTC_NOT_COMPLETE);
    if (failed) {
        e->counter = e->counter < 0 ? 1 : (int8_t)(e->counter + 1);
        if (e->counter >= BCM_DTC_FAIL_LIMIT) {
            e->counter = BCM_DTC_FAIL_LIMIT;
            if ((e->status & DTC_CONFIRMED) == 0u && e->occurrences < 255u) {
                e->occurrences++;
            }
            e->status = (uint8_t)(e->status | DTC_FAILED | DTC_CONFIRMED);
            e->aging = 0u;
        }
    } else {
        e->counter = e->counter > 0 ? -1 : (int8_t)(e->counter - 1);
        if (e->counter <= -BCM_DTC_PASS_LIMIT) {
            e->counter = (int8_t)-BCM_DTC_PASS_LIMIT;
            e->status = (uint8_t)(e->status & ~DTC_FAILED);
        }
    }
}

uint8_t bcm_dtc_status(bcm_dtc_t id)
{
    if ((unsigned)id >= (unsigned)BCM_DTC_COUNT) {
        return 0u;
    }
    return g_dtc[id].status;
}

uint8_t bcm_dtc_occurrences(bcm_dtc_t id)
{
    if ((unsigned)id >= (unsigned)BCM_DTC_COUNT) {
        return 0u;
    }
    return g_dtc[id].occurrences;
}

void bcm_dtc_operation_cycle(void)
{
    int i;
    for (i = 0; i < (int)BCM_DTC_COUNT; i++) {
        bcm_dtc_entry_t *e = &g_dtc[i];
        if ((e->status & DTC_CONFIRMED) != 0u && (e->status & DTC_FAILED) == 0u) {
            e->aging++;
            if (e->aging >= BCM_DTC_AGING_CYCLES) {
                e->status = (uint8_t)(e->status & ~DTC_CONFIRMED);
                e->aging = 0u;
            }
        }
        e->status = (uint8_t)(e->status | DTC_NOT_COMPLETE);
    }
}

uint8_t bcm_dtc_count_confirmed(void)
{
    uint8_t n = 0u;
    int i;
    for (i = 0; i < (int)BCM_DTC_COUNT; i++) {
        if ((g_dtc[i].status & DTC_CONFIRMED) != 0u) {
            n++;
        }
    }
    return n;
}

void bcm_dtc_freeze(bool frozen)
{
    g_dtc_frozen = frozen;
}

void bcm_dtc_clear(void)
{
    memset(g_dtc, 0, sizeof g_dtc);
}


/* ----------------------------------------------------------------------------------- parameters (NVM) */
static bcm_params_t g_params;
static bool g_params_dirty;

static const bcm_params_t k_default_params = {
    30000u, 15, 1000, 3000, BCM_PARAMS_VERSION, 1u, 1u, 0u, 0u
};

static uint8_t params_crc(const bcm_params_t *p)
{
    return bcm_crc8_j1850((const uint8_t *)p, (uint8_t)offsetof(bcm_params_t, crc));
}

bool bcm_params_load(void)
{
    bcm_params_t stored;
    if (!nvm_read(BCM_NVM_BLOCK_PARAMS, (uint8_t *)&stored, (uint16_t)sizeof stored)) {
        g_params = k_default_params;
        bcm_dtc_report(BCM_DTC_NVM, true);
        return false;
    }
    if (stored.version != BCM_PARAMS_VERSION || params_crc(&stored) != stored.crc) {
        g_params = k_default_params;
        g_params_dirty = true;
        return false;
    }
    if (stored.autolock_kph < 5 || stored.autolock_kph > 40 || stored.follow_me_ms > 120000u) {
        g_params = k_default_params;
        g_params_dirty = true;
        return false;
    }
    g_params = stored;
    return true;
}

bool bcm_params_set_autolock(int16_t kph)
{
    if (kph < 5 || kph > 40) {
        return false;
    }
    if (g_params.autolock_kph != kph) {
        g_params.autolock_kph = kph;
        g_params_dirty = true;
    }
    return true;
}

bool bcm_params_set_follow_me(uint32_t ms)
{
    if (ms > 120000u || (ms % 1000u) != 0u) {
        return false;
    }
    if (g_params.follow_me_ms != ms) {
        g_params.follow_me_ms = ms;
        g_params_dirty = true;
    }
    return true;
}

bool bcm_params_store(bcm_power_t power)
{
    if (!g_params_dirty || power != BCM_POWER_OFF) {
        return false;
    }
    g_params.crc = params_crc(&g_params);
    if (!nvm_write(BCM_NVM_BLOCK_PARAMS, (const uint8_t *)&g_params, (uint16_t)sizeof g_params)) {
        bcm_dtc_report(BCM_DTC_NVM, true);
        return false;
    }
    g_params_dirty = false;
    return true;
}

const bcm_params_t *bcm_params(void)
{
    return &g_params;
}

void bcm_params_reset(void)
{
    g_params = k_default_params;
    g_params_dirty = false;
}


/* ------------------------------------------------------------------------------- event queues (generic) */
#define DEFINE_QUEUE(name, type, size)                                                   \
    static type name##_items[(size)];                                                    \
    static uint8_t name##_head;                                                          \
    static uint8_t name##_count;                                                         \
    static uint16_t name##_dropped;                                                      \
    bool name##_push(type item)                                                          \
    {                                                                                    \
        if (name##_count >= (size)) {                                                    \
            if (name##_dropped < 0xFFFFu) {                                              \
                name##_dropped++;                                                        \
            }                                                                            \
            return false;                                                                \
        }                                                                                \
        name##_items[(name##_head + name##_count) % (size)] = item;                      \
        name##_count++;                                                                  \
        return true;                                                                     \
    }                                                                                    \
    bool name##_pop(type *item)                                                          \
    {                                                                                    \
        if (name##_count == 0u || item == NULL) {                                        \
            return false;                                                                \
        }                                                                                \
        *item = name##_items[name##_head];                                               \
        name##_head = (uint8_t)((name##_head + 1u) % (size));                            \
        name##_count--;                                                                  \
        return true;                                                                     \
    }                                                                                    \
    uint8_t name##_size(void)                                                            \
    {                                                                                    \
        return name##_count;                                                             \
    }                                                                                    \
    uint16_t name##_drops(void)                                                          \
    {                                                                                    \
        return name##_dropped;                                                           \
    }                                                                                    \
    void name##_clear(void)                                                              \
    {                                                                                    \
        name##_head = 0u;                                                                \
        name##_count = 0u;                                                               \
        name##_dropped = 0u;                                                             \
    }

DEFINE_QUEUE(evq_input, bcm_event_t, 8u)
DEFINE_QUEUE(evq_can_tx, uint32_t, 4u)


/* --------------------------------------------------------------------------------------------- key fob */
static uint32_t g_fob_counter;
static uint8_t g_fob_failures;
static uint16_t g_fob_block_ms;

bcm_lock_cmd_t bcm_fob_frame(const uint8_t *frame, uint8_t len, uint32_t fob_id, uint16_t dt_ms)
{
    uint32_t id;
    uint32_t counter;
    uint8_t button;
    if (g_fob_block_ms > 0u) {
        g_fob_block_ms = g_fob_block_ms > dt_ms ? (uint16_t)(g_fob_block_ms - dt_ms) : 0u;
        return BCM_LOCK_NONE;
    }
    if (frame == NULL || len != 10u || bcm_crc8_j1850(frame, 9u) != frame[9]) {
        return BCM_LOCK_NONE;
    }
    id = (uint32_t)frame[0] | ((uint32_t)frame[1] << 8) | ((uint32_t)frame[2] << 16) | ((uint32_t)frame[3] << 24);
    counter = (uint32_t)frame[4] | ((uint32_t)frame[5] << 8) | ((uint32_t)frame[6] << 16) | ((uint32_t)frame[7] << 24);
    button = frame[8];
    if (id != fob_id) {
        return BCM_LOCK_NONE;
    }
    if (counter <= g_fob_counter || counter - g_fob_counter > BCM_FOB_WINDOW) {
        g_fob_failures++;
        if (g_fob_failures >= BCM_FOB_MAX_FAILURES) {
            g_fob_block_ms = BCM_FOB_BLOCK_MS;
            g_fob_failures = 0u;
        }
        return BCM_LOCK_NONE;
    }
    g_fob_counter = counter;
    g_fob_failures = 0u;
    switch (button) {
    case 1u:
        return BCM_LOCK;
    case 2u:
        return BCM_UNLOCK;
    default:
        return BCM_LOCK_NONE;
    }
}

void bcm_fob_resync(uint32_t counter)
{
    g_fob_counter = counter;
    g_fob_failures = 0u;
    g_fob_block_ms = 0u;
}


/* -------------------------------------------------------------------------------- diagnostic services */
static bcm_session_t g_session = BCM_SESSION_DEFAULT;
static bool g_unlocked;
static uint16_t g_seed;
static uint8_t g_key_attempts;
static uint16_t g_session_ms;

static uint16_t seed_key(uint16_t seed)
{
    uint16_t key = (uint16_t)(seed ^ 0x5A3Cu);
    key = (uint16_t)((key << 3) | (key >> 13));
    return (uint16_t)(key + 0x1234u);
}

uint8_t bcm_diag_session(bcm_session_t requested, int16_t speed_kph)
{
    if (requested == BCM_SESSION_PROGRAMMING && speed_kph > 0) {
        return BCM_NRC_CONDITIONS;
    }
    if (requested != BCM_SESSION_DEFAULT && requested != BCM_SESSION_EXTENDED && requested != BCM_SESSION_PROGRAMMING) {
        return BCM_NRC_SUBFUNCTION;
    }
    g_session = requested;
    g_session_ms = 0u;
    g_unlocked = false;
    bcm_dtc_freeze(requested == BCM_SESSION_PROGRAMMING);
    return BCM_NRC_OK;
}

void bcm_diag_tick(uint16_t dt_ms, bool tester_present)
{
    if (g_session == BCM_SESSION_DEFAULT) {
        return;
    }
    if (tester_present) {
        g_session_ms = 0u;
        return;
    }
    g_session_ms = (uint16_t)(g_session_ms + dt_ms);
    if (g_session_ms >= BCM_S3_TIMEOUT_MS) {
        g_session = BCM_SESSION_DEFAULT;
        g_unlocked = false;
        bcm_dtc_freeze(false);
    }
}

uint8_t bcm_diag_request_seed(uint16_t random, uint16_t *seed)
{
    if (seed == NULL) {
        return BCM_NRC_FORMAT;
    }
    if (g_session == BCM_SESSION_DEFAULT) {
        return BCM_NRC_SESSION;
    }
    if (g_key_attempts >= BCM_MAX_KEY_ATTEMPTS) {
        return BCM_NRC_EXCEEDED;
    }
    g_seed = g_unlocked ? 0u : (uint16_t)(random | 1u);
    *seed = g_seed;
    return BCM_NRC_OK;
}

uint8_t bcm_diag_send_key(uint16_t key)
{
    if (g_session == BCM_SESSION_DEFAULT) {
        return BCM_NRC_SESSION;
    }
    if (g_seed == 0u) {
        return BCM_NRC_SEQUENCE;
    }
    if (key != seed_key(g_seed)) {
        g_key_attempts++;
        g_seed = 0u;
        return g_key_attempts >= BCM_MAX_KEY_ATTEMPTS ? BCM_NRC_EXCEEDED : BCM_NRC_INVALID_KEY;
    }
    g_unlocked = true;
    g_key_attempts = 0u;
    g_seed = 0u;
    return BCM_NRC_OK;
}

uint8_t bcm_diag_read(uint16_t did, uint8_t *out, uint8_t size, uint8_t *len)
{
    const bcm_params_t *p = bcm_params();
    if (out == NULL || len == NULL) {
        return BCM_NRC_FORMAT;
    }
    switch (did) {
    case 0xF190u:
        if (size < 4u) {
            return BCM_NRC_FORMAT;
        }
        out[0] = 'B';
        out[1] = 'C';
        out[2] = 'M';
        out[3] = (uint8_t)p->version;
        *len = 4u;
        return BCM_NRC_OK;
    case 0x0101u:
        if (size < 2u) {
            return BCM_NRC_FORMAT;
        }
        out[0] = (uint8_t)((uint16_t)p->autolock_kph >> 8);
        out[1] = (uint8_t)p->autolock_kph;
        *len = 2u;
        return BCM_NRC_OK;
    case 0x0102u:
        if (size < 1u) {
            return BCM_NRC_FORMAT;
        }
        out[0] = bcm_dtc_count_confirmed();
        *len = 1u;
        return BCM_NRC_OK;
    default:
        return BCM_NRC_OUT_OF_RANGE;
    }
}

uint8_t bcm_diag_write(uint16_t did, const uint8_t *data, uint8_t len)
{
    if (data == NULL) {
        return BCM_NRC_FORMAT;
    }
    if (g_session != BCM_SESSION_EXTENDED) {
        return BCM_NRC_SESSION;
    }
    if (!g_unlocked) {
        return BCM_NRC_SECURITY;
    }
    switch (did) {
    case 0x0101u:
        if (len != 2u) {
            return BCM_NRC_FORMAT;
        }
        return bcm_params_set_autolock((int16_t)(((uint16_t)data[0] << 8) | data[1])) ? BCM_NRC_OK : BCM_NRC_OUT_OF_RANGE;
    case 0x0103u:
        if (len != 4u) {
            return BCM_NRC_FORMAT;
        }
        return bcm_params_set_follow_me(((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
                                        ((uint32_t)data[2] << 8) | data[3]) ? BCM_NRC_OK : BCM_NRC_OUT_OF_RANGE;
    default:
        return BCM_NRC_OUT_OF_RANGE;
    }
}

bcm_session_t bcm_diag_current_session(void)
{
    return g_session;
}

void bcm_diag_reset(void)
{
    g_session = BCM_SESSION_DEFAULT;
    g_unlocked = false;
    g_seed = 0u;
    g_key_attempts = 0u;
    g_session_ms = 0u;
}


/* ------------------------------------------------------------------------------------- task scheduler */
static uint32_t g_now_ms;
static uint16_t g_overruns;

static void task_10ms(const bcm_inputs_t *in)
{
    bool hazard = sw_hazard_debounce(in->hazard, 10u);
    (void)bcm_turn_request(in->turn_lever, hazard, in->turn_tap, in->crash);
    (void)bcm_turn_flash(10u, false);
    (void)bcm_window_fl(in->win_fl, in->express, 10u);
    (void)bcm_window_fr(in->win_fr, in->express, 10u);
    (void)bcm_window_rl(in->win_rl, in->express, 10u);
    (void)bcm_window_rr(in->win_rr, in->express, 10u);
    (void)bcm_horn(sw_horn_debounce(in->horn, 10u), BCM_HORN_NONE, 10u);
}

static void task_100ms(const bcm_inputs_t *in)
{
    bool doors = in->doors != 0u;
    bcm_power_t power = bcm_power_step(in->key, sw_brake_debounce(in->brake, 100u), in->engine_running, 100u);
    bool auto_on = bcm_lights_auto(in->lux, in->auto_lights, in->wiper == BCM_WIPER_HIGH, power, 100u);
    (void)bcm_low_beam(in->light_switch, auto_on, (in->doors & 1u) != 0u, power, 100u);
    (void)bcm_wiper_front(in->wiper, in->speed_kph, in->rain, in->washer, 100u);
    (void)bcm_wiper_rear(in->rear_wiper, in->speed_kph, 0, false, 100u);
    (void)bcm_lock_command(in->lock, in->speed_kph, in->crash, doors, 100u);
    (void)bcm_interior_light(doors, false, bcm_lock_command(BCM_LOCK_NONE, in->speed_kph, false, doors, 0u), 100u);
    (void)bcm_load_shed(in->battery_mv, in->engine_running);
    bcm_diag_tick(100u, in->tester_present);
}

uint16_t bcm_schedule(const bcm_inputs_t *in, uint32_t now_ms)
{
    uint32_t elapsed;
    uint16_t ran = 0u;
    if (in == NULL) {
        return 0u;
    }
    if (now_ms < g_now_ms) {
        g_now_ms = now_ms;
        return 0u;
    }
    elapsed = now_ms - g_now_ms;
    if (elapsed > 1000u) {
        g_overruns++;
        g_now_ms = now_ms;
        bcm_dtc_report(BCM_DTC_SCHEDULER, true);
        return 0u;
    }
    while (elapsed >= 10u) {
        task_10ms(in);
        ran++;
        g_now_ms += 10u;
        elapsed -= 10u;
        if ((g_now_ms % 100u) == 0u) {
            task_100ms(in);
            ran++;
        }
    }
    return ran;
}

uint16_t bcm_schedule_overruns(void)
{
    return g_overruns;
}

void bcm_schedule_reset(void)
{
    g_now_ms = 0u;
    g_overruns = 0u;
}


/* ---------------------------------------------------------------------------------------- climate */
static bool g_ac_on;
static bool g_recirc;
static uint16_t g_compressor_off_ms;
static uint32_t g_recirc_ms;

uint8_t bcm_climate_fan(int16_t cabin_temp, int16_t set_temp, bool auto_mode, uint8_t manual_level)
{
    int16_t filtered = ema_cabin_temp_update(cabin_temp);
    int16_t error = (int16_t)BCM_ABS(set_temp - filtered);
    if (!auto_mode) {
        return manual_level > 7u ? 100u : (uint8_t)(manual_level * 14u);
    }
    if (error < BCM_CLIMATE_DEADBAND) {
        return BCM_FAN_MIN_DUTY;
    }
    return (uint8_t)clamp_i16(lut_fan_duty_lookup(error), BCM_FAN_MIN_DUTY, 100);
}

bool bcm_climate_ac(bool request, int16_t outside_temp, int16_t evaporator_temp, uint16_t pressure_kpa, uint16_t dt_ms)
{
    if (g_compressor_off_ms > 0u) {
        g_compressor_off_ms = g_compressor_off_ms > dt_ms ? (uint16_t)(g_compressor_off_ms - dt_ms) : 0u;
        g_ac_on = false;
        return false;
    }
    if (!request || outside_temp < BCM_AC_MIN_OUTSIDE) {
        g_ac_on = false;
    } else if (evaporator_temp < BCM_EVAP_FREEZE || pressure_kpa > BCM_AC_MAX_KPA || pressure_kpa < BCM_AC_MIN_KPA) {
        g_ac_on = false;
        g_compressor_off_ms = BCM_COMPRESSOR_LOCKOUT_MS;
    } else {
        g_ac_on = true;
    }
    return g_ac_on;
}

bool bcm_climate_recirc(bool button, bool defrost, int16_t air_quality, uint16_t dt_ms)
{
    if (defrost) {
        g_recirc = false;
        g_recirc_ms = 0u;
    } else if (button) {
        g_recirc = !g_recirc;
        g_recirc_ms = 0u;
    } else if (air_quality > BCM_AIR_POLLUTED) {
        g_recirc = true;
    } else if (g_recirc) {
        g_recirc_ms += dt_ms;
        if (g_recirc_ms > BCM_RECIRC_MAX_MS) {
            g_recirc = false;
            g_recirc_ms = 0u;
        }
    }
    return g_recirc;
}

void bcm_climate_reset(void)
{
    g_ac_on = false;
    g_recirc = false;
    g_compressor_off_ms = 0u;
    g_recirc_ms = 0u;
    ema_cabin_temp_reset();
}


/* ----------------------------------------------------------------------------- tire pressure fl */
static uint16_t tpms_fl_kpa;
static uint16_t tpms_fl_age_ms;
static uint8_t tpms_fl_warn;

uint8_t bcm_tpms_fl(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms)
{
    int32_t compensated;
    if (!received) {
        if (tpms_fl_age_ms < BCM_TPMS_TIMEOUT_MS) {
            tpms_fl_age_ms = (uint16_t)(tpms_fl_age_ms + dt_ms);
        } else {
            tpms_fl_warn = BCM_TPMS_SENSOR_LOST;
        }
        return tpms_fl_warn;
    }
    tpms_fl_age_ms = 0u;
    compensated = (int32_t)kpa * 293 / (273 + (int32_t)temp_c);
    tpms_fl_kpa = (uint16_t)clamp_i32(compensated, 0, 600);
    if (tpms_fl_kpa < BCM_TPMS_SEVERE_KPA) {
        tpms_fl_warn = BCM_TPMS_SEVERE;
    } else if (tpms_fl_kpa < BCM_TPMS_LOW_KPA) {
        tpms_fl_warn = BCM_TPMS_LOW;
    } else if (tpms_fl_kpa > BCM_TPMS_LOW_KPA + BCM_TPMS_HYST_KPA || tpms_fl_warn == BCM_TPMS_SENSOR_LOST) {
        tpms_fl_warn = BCM_TPMS_OK;
    }
    return tpms_fl_warn;
}

uint16_t bcm_tpms_fl_pressure(void)
{
    return tpms_fl_kpa;
}

void bcm_tpms_fl_reset(void)
{
    tpms_fl_kpa = 0u;
    tpms_fl_age_ms = 0u;
    tpms_fl_warn = BCM_TPMS_OK;
}


/* ----------------------------------------------------------------------------- tire pressure fr */
static uint16_t tpms_fr_kpa;
static uint16_t tpms_fr_age_ms;
static uint8_t tpms_fr_warn;

uint8_t bcm_tpms_fr(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms)
{
    int32_t compensated;
    if (!received) {
        if (tpms_fr_age_ms < BCM_TPMS_TIMEOUT_MS) {
            tpms_fr_age_ms = (uint16_t)(tpms_fr_age_ms + dt_ms);
        } else {
            tpms_fr_warn = BCM_TPMS_SENSOR_LOST;
        }
        return tpms_fr_warn;
    }
    tpms_fr_age_ms = 0u;
    compensated = (int32_t)kpa * 293 / (273 + (int32_t)temp_c);
    tpms_fr_kpa = (uint16_t)clamp_i32(compensated, 0, 600);
    if (tpms_fr_kpa < BCM_TPMS_SEVERE_KPA) {
        tpms_fr_warn = BCM_TPMS_SEVERE;
    } else if (tpms_fr_kpa < BCM_TPMS_LOW_KPA) {
        tpms_fr_warn = BCM_TPMS_LOW;
    } else if (tpms_fr_kpa > BCM_TPMS_LOW_KPA + BCM_TPMS_HYST_KPA || tpms_fr_warn == BCM_TPMS_SENSOR_LOST) {
        tpms_fr_warn = BCM_TPMS_OK;
    }
    return tpms_fr_warn;
}

uint16_t bcm_tpms_fr_pressure(void)
{
    return tpms_fr_kpa;
}

void bcm_tpms_fr_reset(void)
{
    tpms_fr_kpa = 0u;
    tpms_fr_age_ms = 0u;
    tpms_fr_warn = BCM_TPMS_OK;
}


/* ----------------------------------------------------------------------------- tire pressure rl */
static uint16_t tpms_rl_kpa;
static uint16_t tpms_rl_age_ms;
static uint8_t tpms_rl_warn;

uint8_t bcm_tpms_rl(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms)
{
    int32_t compensated;
    if (!received) {
        if (tpms_rl_age_ms < BCM_TPMS_TIMEOUT_MS) {
            tpms_rl_age_ms = (uint16_t)(tpms_rl_age_ms + dt_ms);
        } else {
            tpms_rl_warn = BCM_TPMS_SENSOR_LOST;
        }
        return tpms_rl_warn;
    }
    tpms_rl_age_ms = 0u;
    compensated = (int32_t)kpa * 293 / (273 + (int32_t)temp_c);
    tpms_rl_kpa = (uint16_t)clamp_i32(compensated, 0, 600);
    if (tpms_rl_kpa < BCM_TPMS_SEVERE_KPA) {
        tpms_rl_warn = BCM_TPMS_SEVERE;
    } else if (tpms_rl_kpa < BCM_TPMS_LOW_KPA) {
        tpms_rl_warn = BCM_TPMS_LOW;
    } else if (tpms_rl_kpa > BCM_TPMS_LOW_KPA + BCM_TPMS_HYST_KPA || tpms_rl_warn == BCM_TPMS_SENSOR_LOST) {
        tpms_rl_warn = BCM_TPMS_OK;
    }
    return tpms_rl_warn;
}

uint16_t bcm_tpms_rl_pressure(void)
{
    return tpms_rl_kpa;
}

void bcm_tpms_rl_reset(void)
{
    tpms_rl_kpa = 0u;
    tpms_rl_age_ms = 0u;
    tpms_rl_warn = BCM_TPMS_OK;
}


/* ----------------------------------------------------------------------------- tire pressure rr */
static uint16_t tpms_rr_kpa;
static uint16_t tpms_rr_age_ms;
static uint8_t tpms_rr_warn;

uint8_t bcm_tpms_rr(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms)
{
    int32_t compensated;
    if (!received) {
        if (tpms_rr_age_ms < BCM_TPMS_TIMEOUT_MS) {
            tpms_rr_age_ms = (uint16_t)(tpms_rr_age_ms + dt_ms);
        } else {
            tpms_rr_warn = BCM_TPMS_SENSOR_LOST;
        }
        return tpms_rr_warn;
    }
    tpms_rr_age_ms = 0u;
    compensated = (int32_t)kpa * 293 / (273 + (int32_t)temp_c);
    tpms_rr_kpa = (uint16_t)clamp_i32(compensated, 0, 600);
    if (tpms_rr_kpa < BCM_TPMS_SEVERE_KPA) {
        tpms_rr_warn = BCM_TPMS_SEVERE;
    } else if (tpms_rr_kpa < BCM_TPMS_LOW_KPA) {
        tpms_rr_warn = BCM_TPMS_LOW;
    } else if (tpms_rr_kpa > BCM_TPMS_LOW_KPA + BCM_TPMS_HYST_KPA || tpms_rr_warn == BCM_TPMS_SENSOR_LOST) {
        tpms_rr_warn = BCM_TPMS_OK;
    }
    return tpms_rr_warn;
}

uint16_t bcm_tpms_rr_pressure(void)
{
    return tpms_rr_kpa;
}

void bcm_tpms_rr_reset(void)
{
    tpms_rr_kpa = 0u;
    tpms_rr_age_ms = 0u;
    tpms_rr_warn = BCM_TPMS_OK;
}


uint8_t bcm_tpms_worst(void)
{
    uint8_t worst = BCM_TPMS_OK;
    uint8_t w[4];
    int i;
    w[0] = bcm_tpms_fl(0u, 0, false, 0u);
    w[1] = bcm_tpms_fr(0u, 0, false, 0u);
    w[2] = bcm_tpms_rl(0u, 0, false, 0u);
    w[3] = bcm_tpms_rr(0u, 0, false, 0u);
    for (i = 0; i < 4; i++) {
        if (w[i] > worst) {
            worst = w[i];
        }
    }
    return worst;
}


/* -------------------------------------------------------------------------------------- anti-theft */
static bcm_alarm_t g_alarm = BCM_ALARM_DISARMED;
static uint16_t g_alarm_ms;
static uint8_t g_alarm_triggers;

bcm_alarm_t bcm_alarm_step(bcm_lock_cmd_t fob, bool any_door_open, bool intrusion, bool tilt, bool panic, uint16_t dt_ms)
{
    switch (g_alarm) {
    case BCM_ALARM_DISARMED:
        if (fob == BCM_LOCK && !any_door_open) {
            g_alarm = BCM_ALARM_ARMING;
            g_alarm_ms = 0u;
        } else if (panic) {
            g_alarm = BCM_ALARM_PANIC;
            g_alarm_ms = 0u;
        }
        break;
    case BCM_ALARM_ARMING:
        g_alarm_ms = (uint16_t)(g_alarm_ms + dt_ms);
        if (fob == BCM_UNLOCK) {
            g_alarm = BCM_ALARM_DISARMED;
        } else if (g_alarm_ms >= BCM_ALARM_ARM_DELAY_MS) {
            g_alarm = BCM_ALARM_ARMED;
            g_alarm_triggers = 0u;
        }
        break;
    case BCM_ALARM_ARMED:
        if (fob == BCM_UNLOCK) {
            g_alarm = BCM_ALARM_DISARMED;
        } else if (any_door_open || intrusion || tilt) {
            if (g_alarm_triggers < BCM_ALARM_MAX_TRIGGERS) {
                g_alarm = BCM_ALARM_TRIGGERED;
                g_alarm_ms = 0u;
                g_alarm_triggers++;
            }
        }
        break;
    case BCM_ALARM_TRIGGERED:
        g_alarm_ms = (uint16_t)(g_alarm_ms + dt_ms);
        if (fob == BCM_UNLOCK) {
            g_alarm = BCM_ALARM_DISARMED;
        } else if (g_alarm_ms >= BCM_ALARM_SOUND_MS) {
            g_alarm = BCM_ALARM_ARMED;
        }
        break;
    case BCM_ALARM_PANIC:
        g_alarm_ms = (uint16_t)(g_alarm_ms + dt_ms);
        if (panic || fob == BCM_UNLOCK || g_alarm_ms >= BCM_ALARM_SOUND_MS) {
            g_alarm = BCM_ALARM_DISARMED;
        }
        break;
    default:
        g_alarm = BCM_ALARM_DISARMED;
        break;
    }
    (void)bcm_horn(false, (g_alarm == BCM_ALARM_TRIGGERED || g_alarm == BCM_ALARM_PANIC) ? BCM_HORN_ALARM : BCM_HORN_NONE, dt_ms);
    return g_alarm;
}

void bcm_alarm_reset(void)
{
    g_alarm = BCM_ALARM_DISARMED;
    g_alarm_ms = 0u;
    g_alarm_triggers = 0u;
}


/* ---------------------------------------------------------------------------------- power tailgate */
static int16_t g_gate_pos;
static bcm_gate_t g_gate = BCM_GATE_CLOSED;

bcm_gate_t bcm_tailgate(bool button, bool kick, bool key_nearby, bool locked, bool obstacle, int16_t speed_kph)
{
    bool request = button || (kick && key_nearby);
    if (speed_kph > 3) {
        if (g_gate == BCM_GATE_OPENING || g_gate == BCM_GATE_CLOSING) {
            g_gate = BCM_GATE_STOPPED;
        }
        return g_gate;
    }
    if (obstacle && (g_gate == BCM_GATE_OPENING || g_gate == BCM_GATE_CLOSING)) {
        g_gate = BCM_GATE_STOPPED;
        return g_gate;
    }
    switch (g_gate) {
    case BCM_GATE_CLOSED:
        if (request && !locked) {
            g_gate = BCM_GATE_OPENING;
        }
        break;
    case BCM_GATE_OPENING:
        g_gate_pos = (int16_t)(g_gate_pos + 5);
        if (request) {
            g_gate = BCM_GATE_STOPPED;
        } else if (g_gate_pos >= BCM_GATE_OPEN_POS) {
            g_gate_pos = BCM_GATE_OPEN_POS;
            g_gate = BCM_GATE_OPEN;
        }
        break;
    case BCM_GATE_OPEN:
        if (request) {
            g_gate = BCM_GATE_CLOSING;
        }
        break;
    case BCM_GATE_CLOSING:
        g_gate_pos = (int16_t)(g_gate_pos - 5);
        if (request) {
            g_gate = BCM_GATE_STOPPED;
        } else if (g_gate_pos <= 0) {
            g_gate_pos = 0;
            g_gate = BCM_GATE_CLOSED;
        }
        break;
    case BCM_GATE_STOPPED:
        if (request) {
            g_gate = g_gate_pos > BCM_GATE_OPEN_POS / 2 ? BCM_GATE_CLOSING : BCM_GATE_OPENING;
        }
        break;
    default:
        g_gate = BCM_GATE_STOPPED;
        break;
    }
    return g_gate;
}

int16_t bcm_tailgate_position(void)
{
    return g_gate_pos;
}

void bcm_tailgate_reset(void)
{
    g_gate_pos = 0;
    g_gate = BCM_GATE_CLOSED;
}


/* ----------------------------------------------------------------------------------------- sunroof */
static int16_t g_roof_pos;
static uint16_t g_roof_reverse_ms;

int16_t bcm_sunroof(bool open_sw, bool close_sw, int16_t rain_level, bool locked, uint16_t motor_ma, uint16_t dt_ms)
{
    bool closing = false;
    if (g_roof_reverse_ms > 0u) {
        g_roof_reverse_ms = g_roof_reverse_ms > dt_ms ? (uint16_t)(g_roof_reverse_ms - dt_ms) : 0u;
        g_roof_pos = clamp_i16((int16_t)(g_roof_pos + 10), 0, BCM_ROOF_OPEN);
        return g_roof_pos;
    }
    if ((rain_level >= BCM_RAIN_LIGHT || locked) && g_roof_pos > 0) {
        closing = true;
    } else if (close_sw && !open_sw && g_roof_pos > 0) {
        closing = true;
    } else if (open_sw && !close_sw && g_roof_pos < BCM_ROOF_OPEN) {
        g_roof_pos = clamp_i16((int16_t)(g_roof_pos + 10), 0, BCM_ROOF_OPEN);
    }
    if (closing) {
        if (motor_ma > BCM_ROOF_PINCH_MA && g_roof_pos > BCM_ROOF_PINCH_ZONE) {
            g_roof_reverse_ms = BCM_ROOF_REVERSE_MS;
        } else {
            g_roof_pos = clamp_i16((int16_t)(g_roof_pos - 10), 0, BCM_ROOF_OPEN);
        }
    }
    return g_roof_pos;
}

void bcm_sunroof_set_position(int16_t pos)
{
    g_roof_pos = clamp_i16(pos, 0, BCM_ROOF_OPEN);
}

void bcm_sunroof_reset(void)
{
    g_roof_pos = 0;
    g_roof_reverse_ms = 0u;
}


/* ------------------------------------------------------------------------------------- immobilizer */
static uint32_t g_challenge;
static bool g_immo_released;
static uint8_t g_immo_fails;

static uint32_t immo_response(uint32_t challenge, uint32_t secret)
{
    uint32_t x = challenge ^ secret;
    int round;
    for (round = 0; round < 4; round++) {
        x = (x << 5) | (x >> 27);
        x ^= 0x9E3779B9u + (uint32_t)round;
    }
    return x;
}

uint32_t bcm_immo_challenge(uint32_t random)
{
    g_challenge = random == 0u ? 0x13579BDFu : random;
    g_immo_released = false;
    return g_challenge;
}

bool bcm_immo_response(uint32_t response, uint32_t secret)
{
    if (g_challenge == 0u || g_immo_fails >= BCM_IMMO_MAX_FAILS) {
        return false;
    }
    if (response == immo_response(g_challenge, secret)) {
        g_immo_released = true;
        g_immo_fails = 0u;
    } else {
        g_immo_fails++;
        g_immo_released = false;
    }
    g_challenge = 0u;
    return g_immo_released;
}

bool bcm_immo_released(void)
{
    return g_immo_released;
}

uint32_t bcm_immo_expected(uint32_t challenge, uint32_t secret)
{
    return immo_response(challenge, secret);
}

void bcm_immo_reset(void)
{
    g_challenge = 0u;
    g_immo_released = false;
    g_immo_fails = 0u;
}


/* ------------------------------------------------------------------------ park sensor rear_left */
static uint16_t pdc_rear_left_cm;
static uint8_t pdc_rear_left_bad;

uint16_t bcm_pdc_rear_left(uint16_t echo_us, bool echo_valid)
{
    uint32_t cm;
    if (!echo_valid || echo_us == 0u) {
        if (pdc_rear_left_bad < 255u) {
            pdc_rear_left_bad++;
        }
        return pdc_rear_left_bad >= BCM_PDC_BAD_LIMIT ? BCM_PDC_FAULT_CM : pdc_rear_left_cm;
    }
    pdc_rear_left_bad = 0u;
    cm = (uint32_t)echo_us * 343u / 20000u;
    pdc_rear_left_cm = cm > BCM_PDC_MAX_CM ? BCM_PDC_MAX_CM : (uint16_t)cm;
    return pdc_rear_left_cm;
}

void bcm_pdc_rear_left_reset(void)
{
    pdc_rear_left_cm = BCM_PDC_MAX_CM;
    pdc_rear_left_bad = 0u;
}


/* ------------------------------------------------------------------------ park sensor rear_center */
static uint16_t pdc_rear_center_cm;
static uint8_t pdc_rear_center_bad;

uint16_t bcm_pdc_rear_center(uint16_t echo_us, bool echo_valid)
{
    uint32_t cm;
    if (!echo_valid || echo_us == 0u) {
        if (pdc_rear_center_bad < 255u) {
            pdc_rear_center_bad++;
        }
        return pdc_rear_center_bad >= BCM_PDC_BAD_LIMIT ? BCM_PDC_FAULT_CM : pdc_rear_center_cm;
    }
    pdc_rear_center_bad = 0u;
    cm = (uint32_t)echo_us * 343u / 20000u;
    pdc_rear_center_cm = cm > BCM_PDC_MAX_CM ? BCM_PDC_MAX_CM : (uint16_t)cm;
    return pdc_rear_center_cm;
}

void bcm_pdc_rear_center_reset(void)
{
    pdc_rear_center_cm = BCM_PDC_MAX_CM;
    pdc_rear_center_bad = 0u;
}


/* ------------------------------------------------------------------------ park sensor rear_right */
static uint16_t pdc_rear_right_cm;
static uint8_t pdc_rear_right_bad;

uint16_t bcm_pdc_rear_right(uint16_t echo_us, bool echo_valid)
{
    uint32_t cm;
    if (!echo_valid || echo_us == 0u) {
        if (pdc_rear_right_bad < 255u) {
            pdc_rear_right_bad++;
        }
        return pdc_rear_right_bad >= BCM_PDC_BAD_LIMIT ? BCM_PDC_FAULT_CM : pdc_rear_right_cm;
    }
    pdc_rear_right_bad = 0u;
    cm = (uint32_t)echo_us * 343u / 20000u;
    pdc_rear_right_cm = cm > BCM_PDC_MAX_CM ? BCM_PDC_MAX_CM : (uint16_t)cm;
    return pdc_rear_right_cm;
}

void bcm_pdc_rear_right_reset(void)
{
    pdc_rear_right_cm = BCM_PDC_MAX_CM;
    pdc_rear_right_bad = 0u;
}


/* ------------------------------------------------------------------------ park sensor front_center */
static uint16_t pdc_front_center_cm;
static uint8_t pdc_front_center_bad;

uint16_t bcm_pdc_front_center(uint16_t echo_us, bool echo_valid)
{
    uint32_t cm;
    if (!echo_valid || echo_us == 0u) {
        if (pdc_front_center_bad < 255u) {
            pdc_front_center_bad++;
        }
        return pdc_front_center_bad >= BCM_PDC_BAD_LIMIT ? BCM_PDC_FAULT_CM : pdc_front_center_cm;
    }
    pdc_front_center_bad = 0u;
    cm = (uint32_t)echo_us * 343u / 20000u;
    pdc_front_center_cm = cm > BCM_PDC_MAX_CM ? BCM_PDC_MAX_CM : (uint16_t)cm;
    return pdc_front_center_cm;
}

void bcm_pdc_front_center_reset(void)
{
    pdc_front_center_cm = BCM_PDC_MAX_CM;
    pdc_front_center_bad = 0u;
}


uint16_t bcm_pdc_beep_interval(uint16_t nearest_cm, bool reverse, int16_t speed_kph)
{
    if (!reverse || speed_kph > BCM_PDC_MAX_KPH || nearest_cm == BCM_PDC_FAULT_CM) {
        return 0u;
    }
    if (nearest_cm <= BCM_PDC_CONTINUOUS_CM) {
        return 1u;
    }
    if (nearest_cm >= BCM_PDC_MAX_CM) {
        return 0u;
    }
    return (uint16_t)(100u + (uint32_t)(nearest_cm - BCM_PDC_CONTINUOUS_CM) * 900u /
                      (BCM_PDC_MAX_CM - BCM_PDC_CONTINUOUS_CM));
}


/* ------------------------------------------------------------------------------------- signal lamps */
static uint16_t g_brake_flash_ms;

bool bcm_drl(bool low_beam, bool parking_brake, bcm_power_t power, bool engine_running)
{
    return power == BCM_POWER_RUN && engine_running && !low_beam && !parking_brake;
}

uint8_t bcm_fog_lamps(bool front_sw, bool rear_sw, bool low_beam, int16_t speed_kph)
{
    uint8_t out = 0u;
    if (front_sw && low_beam && speed_kph < BCM_FOG_MAX_KPH) {
        out |= 1u;
    }
    if (rear_sw && low_beam) {
        out |= 2u;
    }
    return out;
}

bool bcm_brake_lamp(bool pedal, int16_t decel_cms2, int16_t speed_kph, uint16_t dt_ms)
{
    if (!pedal) {
        g_brake_flash_ms = 0u;
        return false;
    }
    if (decel_cms2 < BCM_EMERGENCY_DECEL || speed_kph < BCM_EMERGENCY_MIN_KPH) {
        g_brake_flash_ms = 0u;
        return true;
    }
    g_brake_flash_ms = (uint16_t)((g_brake_flash_ms + dt_ms) % 200u);
    return g_brake_flash_ms < 100u;
}

bool bcm_reverse_lamp(bool reverse_gear, bcm_power_t power)
{
    return reverse_gear && power != BCM_POWER_OFF;
}


/* ---------------------------------------------------------------------------------------- odometer */
static uint32_t g_odo_m;
static uint32_t g_trip_m;
static uint32_t g_odo_rest_mm;

uint32_t bcm_odometer(uint16_t wheel_pulses, uint16_t mm_per_pulse, bool reset_trip)
{
    uint32_t mm = (uint32_t)wheel_pulses * mm_per_pulse + g_odo_rest_mm;
    if (mm_per_pulse == 0u || mm_per_pulse > BCM_MAX_MM_PER_PULSE) {
        bcm_dtc_report(BCM_DTC_SCHEDULER, false);
        return g_odo_m;
    }
    g_odo_m += mm / 1000u;
    g_trip_m += mm / 1000u;
    g_odo_rest_mm = mm % 1000u;
    if (reset_trip) {
        g_trip_m = 0u;
    }
    if (g_odo_m > BCM_ODO_MAX_M) {
        g_odo_m = BCM_ODO_MAX_M;
    }
    return g_odo_m;
}

uint32_t bcm_trip(void)
{
    return g_trip_m;
}

void bcm_odometer_reset(void)
{
    g_odo_m = 0u;
    g_trip_m = 0u;
    g_odo_rest_mm = 0u;
}


/* ---------------------------------------------------------------------------- steering column lock */
static bool g_column_locked = true;
static uint8_t g_column_retries;

bool bcm_column_lock(bool unlock_request, bool immo_ok, int16_t speed_kph, bool lock_sensor)
{
    if (speed_kph > 0) {
        if (g_column_locked) {
            bcm_dtc_report(BCM_DTC_SCHEDULER, true);
        }
        return g_column_locked;
    }
    if (unlock_request && immo_ok) {
        if (lock_sensor) {
            g_column_retries++;
            if (g_column_retries > BCM_COLUMN_RETRIES) {
                return true;
            }
        } else {
            g_column_locked = false;
            g_column_retries = 0u;
        }
    } else if (!unlock_request) {
        g_column_locked = true;
        g_column_retries = 0u;
    }
    return g_column_locked;
}

void bcm_column_reset(void)
{
    g_column_locked = true;
    g_column_retries = 0u;
}


/* ------------------------------------------------------------------------------ LIN rain/light sensor */
static uint8_t g_lin_errors;
static int16_t g_lin_rain;
static int32_t g_lin_lux;

static uint8_t lin_checksum(uint8_t pid, const uint8_t *data, uint8_t len)
{
    uint16_t sum = pid;
    uint8_t i;
    for (i = 0u; i < len; i++) {
        sum = (uint16_t)(sum + data[i]);
        if (sum > 0xFFu) {
            sum = (uint16_t)(sum - 0xFFu);
        }
    }
    return (uint8_t)(~sum);
}

static uint8_t lin_parity(uint8_t id)
{
    uint8_t p0 = (uint8_t)(((id >> 0) ^ (id >> 1) ^ (id >> 2) ^ (id >> 4)) & 1u);
    uint8_t p1 = (uint8_t)(~((id >> 1) ^ (id >> 3) ^ (id >> 4) ^ (id >> 5)) & 1u);
    return (uint8_t)((id & 0x3Fu) | (p0 << 6) | (p1 << 7));
}

bool bcm_lin_rls_frame(uint8_t pid, const uint8_t *data, uint8_t len)
{
    if (data == NULL || len != 4u || pid != lin_parity(BCM_LIN_RLS_ID)) {
        g_lin_errors++;
        return false;
    }
    if (lin_checksum(pid, data, 3u) != data[3]) {
        g_lin_errors++;
        if (g_lin_errors >= BCM_LIN_MAX_ERRORS) {
            g_lin_rain = 0;
            g_lin_lux = 0;
        }
        return false;
    }
    g_lin_errors = 0u;
    g_lin_rain = (int16_t)(data[0] & 0x0Fu);
    g_lin_lux = ((int32_t)data[1] | ((int32_t)data[2] << 8)) * 4;
    if ((data[0] & 0x80u) != 0u) {
        bcm_dtc_report(BCM_DTC_CAN_CRC, true);
    }
    return true;
}

int16_t bcm_lin_rain(void)
{
    return g_lin_rain;
}

int32_t bcm_lin_lux(void)
{
    return g_lin_lux;
}

uint8_t bcm_lin_pid(uint8_t id)
{
    return lin_parity(id);
}

uint8_t bcm_lin_checksum(uint8_t pid, const uint8_t *data, uint8_t len)
{
    if (data == NULL) {
        return 0u;
    }
    return lin_checksum(pid, data, len);
}

void bcm_lin_reset(void)
{
    g_lin_errors = 0u;
    g_lin_rain = 0;
    g_lin_lux = 0;
}


/* ---------------------------------------------------------------------------------- ambient lighting */
typedef struct {
    uint8_t r;
    uint8_t g;
    uint8_t b;
} rgb_t;

static const rgb_t k_palette[BCM_AMBIENT_COLOURS] = {
    {255u, 255u, 255u}, {255u, 120u, 0u}, {0u, 80u, 255u}, {200u, 0u, 200u}, {0u, 255u, 120u}, {255u, 0u, 0u}
};

uint32_t bcm_ambient_rgb(uint8_t colour, uint8_t brightness, bool night, bool alarm)
{
    rgb_t c;
    uint16_t level = brightness > 100u ? 100u : brightness;
    if (alarm) {
        c = k_palette[5];
        level = 100u;
    } else if (colour < BCM_AMBIENT_COLOURS) {
        c = k_palette[colour];
    } else {
        c = k_palette[0];
    }
    if (night) {
        level = (uint16_t)(level / 2u);
    }
    return ((uint32_t)(c.r * level / 100u) << 16) | ((uint32_t)(c.g * level / 100u) << 8) | (uint32_t)(c.b * level / 100u);
}


/* ---------------------------------------------------------------------------------------- fuel level */
static int32_t g_fuel_dl;
static bool g_fuel_init;
static bool g_fuel_low;

int32_t bcm_fuel_level(uint16_t sender_ohm, int16_t speed_kph, int16_t accel_cms2, bool refuel_lid_open)
{
    int32_t raw;
    if (sender_ohm < BCM_FUEL_OHM_FULL || sender_ohm > BCM_FUEL_OHM_EMPTY + 50u) {
        bcm_dtc_report(BCM_DTC_NVM, true);
        return g_fuel_dl;
    }
    raw = (int32_t)(BCM_FUEL_OHM_EMPTY - (sender_ohm > BCM_FUEL_OHM_EMPTY ? BCM_FUEL_OHM_EMPTY : sender_ohm)) *
          BCM_TANK_DL / (BCM_FUEL_OHM_EMPTY - BCM_FUEL_OHM_FULL);
    if (!g_fuel_init || (refuel_lid_open && speed_kph == 0)) {
        g_fuel_dl = raw;
        g_fuel_init = true;
    } else if (BCM_ABS(accel_cms2) > BCM_FUEL_SLOSH_ACCEL) {
        /* sloshing: keep the last level */
    } else {
        g_fuel_dl += (raw - g_fuel_dl) / 16;
    }
    if (g_fuel_dl < BCM_FUEL_LOW_DL) {
        g_fuel_low = true;
    } else if (g_fuel_dl > BCM_FUEL_LOW_DL + 20) {
        g_fuel_low = false;
    }
    return g_fuel_dl;
}

bool bcm_fuel_low(void)
{
    return g_fuel_low;
}

void bcm_fuel_reset(void)
{
    g_fuel_dl = 0;
    g_fuel_init = false;
    g_fuel_low = false;
}


/* ------------------------------------------------------------------------------- driver seat memory */
typedef struct {
    int16_t fore_aft;
    int16_t height;
    int16_t recline;
    bool valid;
} driver_seat_pos_t;

static driver_seat_pos_t driver_slots[BCM_SEAT_SLOTS];
static driver_seat_pos_t driver_now;
static int8_t driver_recall = -1;

bool bcm_driver_seat_store(uint8_t slot, bcm_power_t power)
{
    if (slot >= BCM_SEAT_SLOTS || power == BCM_POWER_OFF) {
        return false;
    }
    driver_slots[slot] = driver_now;
    driver_slots[slot].valid = true;
    return true;
}

bool bcm_driver_seat_recall(uint8_t slot, int16_t speed_kph)
{
    if (slot >= BCM_SEAT_SLOTS || !driver_slots[slot].valid || speed_kph > 3) {
        driver_recall = -1;
        return false;
    }
    driver_recall = (int8_t)slot;
    return true;
}

static int16_t driver_approach(int16_t now, int16_t target)
{
    if (now < target) {
        return (int16_t)(now + (target - now > BCM_SEAT_STEP ? BCM_SEAT_STEP : target - now));
    }
    if (now > target) {
        return (int16_t)(now - (now - target > BCM_SEAT_STEP ? BCM_SEAT_STEP : now - target));
    }
    return now;
}

bool bcm_driver_seat_move(bool manual_switch, bool obstacle)
{
    const driver_seat_pos_t *t;
    if (driver_recall < 0) {
        return false;
    }
    if (manual_switch || obstacle) {
        driver_recall = -1;
        return false;
    }
    t = &driver_slots[driver_recall];
    driver_now.fore_aft = driver_approach(driver_now.fore_aft, t->fore_aft);
    driver_now.height = driver_approach(driver_now.height, t->height);
    driver_now.recline = driver_approach(driver_now.recline, t->recline);
    if (driver_now.fore_aft == t->fore_aft && driver_now.height == t->height && driver_now.recline == t->recline) {
        driver_recall = -1;
        return false;
    }
    return true;
}

void bcm_driver_seat_set(int16_t fore_aft, int16_t height, int16_t recline)
{
    driver_now.fore_aft = fore_aft;
    driver_now.height = height;
    driver_now.recline = recline;
}

int16_t bcm_driver_seat_fore_aft(void)
{
    return driver_now.fore_aft;
}

void bcm_driver_seat_reset(void)
{
    memset(driver_slots, 0, sizeof driver_slots);
    memset(&driver_now, 0, sizeof driver_now);
    driver_recall = -1;
}


/* ------------------------------------------------------------------------------- passenger seat memory */
typedef struct {
    int16_t fore_aft;
    int16_t height;
    int16_t recline;
    bool valid;
} passenger_seat_pos_t;

static passenger_seat_pos_t passenger_slots[BCM_SEAT_SLOTS];
static passenger_seat_pos_t passenger_now;
static int8_t passenger_recall = -1;

bool bcm_passenger_seat_store(uint8_t slot, bcm_power_t power)
{
    if (slot >= BCM_SEAT_SLOTS || power == BCM_POWER_OFF) {
        return false;
    }
    passenger_slots[slot] = passenger_now;
    passenger_slots[slot].valid = true;
    return true;
}

bool bcm_passenger_seat_recall(uint8_t slot, int16_t speed_kph)
{
    if (slot >= BCM_SEAT_SLOTS || !passenger_slots[slot].valid || speed_kph > 3) {
        passenger_recall = -1;
        return false;
    }
    passenger_recall = (int8_t)slot;
    return true;
}

static int16_t passenger_approach(int16_t now, int16_t target)
{
    if (now < target) {
        return (int16_t)(now + (target - now > BCM_SEAT_STEP ? BCM_SEAT_STEP : target - now));
    }
    if (now > target) {
        return (int16_t)(now - (now - target > BCM_SEAT_STEP ? BCM_SEAT_STEP : now - target));
    }
    return now;
}

bool bcm_passenger_seat_move(bool manual_switch, bool obstacle)
{
    const passenger_seat_pos_t *t;
    if (passenger_recall < 0) {
        return false;
    }
    if (manual_switch || obstacle) {
        passenger_recall = -1;
        return false;
    }
    t = &passenger_slots[passenger_recall];
    passenger_now.fore_aft = passenger_approach(passenger_now.fore_aft, t->fore_aft);
    passenger_now.height = passenger_approach(passenger_now.height, t->height);
    passenger_now.recline = passenger_approach(passenger_now.recline, t->recline);
    if (passenger_now.fore_aft == t->fore_aft && passenger_now.height == t->height && passenger_now.recline == t->recline) {
        passenger_recall = -1;
        return false;
    }
    return true;
}

void bcm_passenger_seat_set(int16_t fore_aft, int16_t height, int16_t recline)
{
    passenger_now.fore_aft = fore_aft;
    passenger_now.height = height;
    passenger_now.recline = recline;
}

int16_t bcm_passenger_seat_fore_aft(void)
{
    return passenger_now.fore_aft;
}

void bcm_passenger_seat_reset(void)
{
    memset(passenger_slots, 0, sizeof passenger_slots);
    memset(&passenger_now, 0, sizeof passenger_now);
    passenger_recall = -1;
}


/* ---------------------------------------------------------------------------------- high beam assist */
static bool g_high_beam;
static uint16_t g_hba_clear_ms;

bool bcm_high_beam(bool lever_high, bool flash, bool assist_on, bool oncoming, bool preceding, int32_t lux,
                   int16_t speed_kph, uint16_t dt_ms)
{
    if (flash) {
        return true;
    }
    if (!assist_on) {
        g_high_beam = lever_high;
        g_hba_clear_ms = 0u;
        return g_high_beam;
    }
    if (!lever_high || lux > BCM_LUX_OFF || speed_kph < BCM_HBA_MIN_KPH) {
        g_high_beam = false;
        g_hba_clear_ms = 0u;
    } else if (oncoming || preceding) {
        g_high_beam = false;
        g_hba_clear_ms = 0u;
    } else if (g_hba_clear_ms < BCM_HBA_CLEAR_MS) {
        g_hba_clear_ms = (uint16_t)(g_hba_clear_ms + dt_ms);
    } else {
        g_high_beam = true;
    }
    return g_high_beam;
}

void bcm_high_beam_reset(void)
{
    g_high_beam = false;
    g_hba_clear_ms = 0u;
}


/* ---------------------------------------------------------------------------------- gateway routing */
typedef struct {
    uint32_t id;
    uint8_t from_bus;
    uint8_t to_bus;
    uint16_t min_gap_ms;
} route_t;

static const route_t k_routes[] = {
    {0x120u, 0u, 1u, 0u}, {0x130u, 0u, 1u, 20u}, {0x140u, 0u, 2u, 0u}, {0x2A0u, 1u, 0u, 100u},
    {0x310u, 1u, 2u, 0u}, {0x385u, 2u, 1u, 500u}, {0x050u, 0u, 3u, 0u}
};
static uint16_t g_route_age[sizeof k_routes / sizeof k_routes[0]];

int8_t bcm_gateway_route(uint32_t id, uint8_t from_bus, bool diagnostic_session, uint16_t now_ms_mod)
{
    size_t i;
    if (from_bus > 3u) {
        return -1;
    }
    if (diagnostic_session && id >= 0x700u && id <= 0x7FFu) {
        return (int8_t)(from_bus == 0u ? 3u : 0u);
    }
    for (i = 0u; i < sizeof k_routes / sizeof k_routes[0]; i++) {
        if (k_routes[i].id == id && k_routes[i].from_bus == from_bus) {
            uint16_t gap = (uint16_t)(now_ms_mod - g_route_age[i]);
            if (k_routes[i].min_gap_ms > 0u && gap < k_routes[i].min_gap_ms) {
                return -2;
            }
            g_route_age[i] = now_ms_mod;
            return (int8_t)k_routes[i].to_bus;
        }
    }
    return -1;
}

void bcm_gateway_reset(void)
{
    memset(g_route_age, 0, sizeof g_route_age);
}


/* ------------------------------------------------------------------------------ network management */
static bcm_nm_t g_nm = BCM_NM_SLEEP;
static uint16_t g_nm_ms;

bcm_nm_t bcm_nm_step(bool local_wake, bool bus_activity, bool keep_awake, uint16_t dt_ms)
{
    switch (g_nm) {
    case BCM_NM_SLEEP:
        if (local_wake || bus_activity) {
            g_nm = BCM_NM_AWAKE;
            g_nm_ms = 0u;
        }
        break;
    case BCM_NM_AWAKE:
        if (keep_awake || local_wake) {
            g_nm_ms = 0u;
        } else {
            g_nm_ms = (uint16_t)(g_nm_ms + dt_ms);
            if (g_nm_ms >= BCM_NM_TIMEOUT_MS) {
                g_nm = BCM_NM_PREPARE_SLEEP;
                g_nm_ms = 0u;
            }
        }
        break;
    case BCM_NM_PREPARE_SLEEP:
        if (bus_activity || local_wake || keep_awake) {
            g_nm = BCM_NM_AWAKE;
            g_nm_ms = 0u;
        } else {
            g_nm_ms = (uint16_t)(g_nm_ms + dt_ms);
            if (g_nm_ms >= BCM_NM_WAIT_SLEEP_MS) {
                g_nm = BCM_NM_SLEEP;
                (void)bcm_params_store(BCM_POWER_OFF);
            }
        }
        break;
    default:
        g_nm = BCM_NM_SLEEP;
        break;
    }
    return g_nm;
}

void bcm_nm_reset(void)
{
    g_nm = BCM_NM_SLEEP;
    g_nm_ms = 0u;
}


/* -------------------------------------------------------------------------------------- supervision */
static uint8_t g_alive_mask;
static uint8_t g_supervision_fails;

void bcm_supervision_checkpoint(uint8_t task)
{
    if (task < 8u) {
        g_alive_mask = (uint8_t)(g_alive_mask | (1u << task));
    }
}

bool bcm_supervision_cycle(uint8_t expected_mask)
{
    bool ok = (g_alive_mask & expected_mask) == expected_mask;
    g_alive_mask = 0u;
    if (ok) {
        g_supervision_fails = 0u;
        return true;
    }
    g_supervision_fails++;
    if (g_supervision_fails >= BCM_SUPERVISION_LIMIT) {
        bcm_dtc_report(BCM_DTC_SCHEDULER, true);
        return false;
    }
    return true;
}

void bcm_supervision_reset(void)
{
    g_alive_mask = 0u;
    g_supervision_fails = 0u;
}

/* ------------------------------------------------------------------------------------- washer fluid */
static uint16_t g_washer_low_ms;

bool bcm_washer_low(bool level_switch_low, int16_t accel_cms2, uint16_t dt_ms)
{
    if (!level_switch_low || BCM_ABS(accel_cms2) > BCM_FUEL_SLOSH_ACCEL) {
        g_washer_low_ms = 0u;
        return false;
    }
    if (g_washer_low_ms < BCM_WASHER_LOW_MS) {
        g_washer_low_ms = (uint16_t)(g_washer_low_ms + dt_ms);
        return false;
    }
    return true;
}

/* -------------------------------------------------------------------------------- rear seat reminder */
static bool g_rear_opened;

bool bcm_rear_seat_reminder(bool rear_door_opened, bcm_power_t power, bool driver_door_open)
{
    if (rear_door_opened && power == BCM_POWER_RUN) {
        g_rear_opened = true;
    }
    if (g_rear_opened && power == BCM_POWER_OFF && driver_door_open) {
        g_rear_opened = false;
        return true;
    }
    return false;
}

void bcm_misc_reset(void)
{
    g_washer_low_ms = 0u;
    g_rear_opened = false;
}


/* ----------------------------------------------------------------------------- sliding door left */
static int16_t slide_left_pos;
static bcm_slide_t slide_left_state;
static uint8_t slide_left_obstacles;

bcm_slide_t bcm_slide_door_left(bool request, bool child_lock_inner, bool inner_request, bool locked, int16_t speed_kph,
                               bool fuel_lid_open)
{
    uint16_t current = hal_adc_read(BCM_ADC_SLIDE_L);
    bool want = request || (inner_request && !child_lock_inner);
    if (speed_kph > 0) {
        if (slide_left_state == BCM_SLIDE_OPENING || slide_left_state == BCM_SLIDE_CLOSING) {
            slide_left_state = BCM_SLIDE_STOPPED;
        }
    } else if ((slide_left_state == BCM_SLIDE_OPENING || slide_left_state == BCM_SLIDE_CLOSING) &&
               current > BCM_SLIDE_LIMIT_MA) {
        slide_left_state = slide_left_state == BCM_SLIDE_CLOSING ? BCM_SLIDE_OPENING : BCM_SLIDE_STOPPED;
        if (slide_left_obstacles < 255u) {
            slide_left_obstacles++;
        }
    } else if (slide_left_state == BCM_SLIDE_OPENING) {
        slide_left_pos = clamp_i16((int16_t)(slide_left_pos + 25), 0, BCM_SLIDE_OPEN_POS);
        if (want) {
            slide_left_state = BCM_SLIDE_STOPPED;
        } else if (slide_left_pos >= BCM_SLIDE_OPEN_POS) {
            slide_left_state = BCM_SLIDE_OPEN;
        }
    } else if (slide_left_state == BCM_SLIDE_CLOSING) {
        slide_left_pos = clamp_i16((int16_t)(slide_left_pos - 25), 0, BCM_SLIDE_OPEN_POS);
        if (want) {
            slide_left_state = BCM_SLIDE_STOPPED;
        } else if (slide_left_pos <= 0) {
            slide_left_state = BCM_SLIDE_CLOSED;
        }
    } else if (want && (slide_left_state == BCM_SLIDE_CLOSED || slide_left_state == BCM_SLIDE_STOPPED) && !locked) {
        bool blocked = fuel_lid_open && slide_left_pos < BCM_SLIDE_OPEN_POS / 4;
        if (!blocked) {
            slide_left_state = BCM_SLIDE_OPENING;
        }
    } else if (want && slide_left_state == BCM_SLIDE_OPEN) {
        slide_left_state = BCM_SLIDE_CLOSING;
    }
    hal_gpio_write(BCM_PIN_SLIDE_L_OPEN, slide_left_state == BCM_SLIDE_OPENING);
    hal_gpio_write(BCM_PIN_SLIDE_L_CLOSE, slide_left_state == BCM_SLIDE_CLOSING);
    return slide_left_state;
}

int16_t bcm_slide_door_left_position(void)
{
    return slide_left_pos;
}

uint8_t bcm_slide_door_left_obstacles(void)
{
    return slide_left_obstacles;
}

void bcm_slide_door_left_reset(void)
{
    slide_left_pos = 0;
    slide_left_state = BCM_SLIDE_CLOSED;
    slide_left_obstacles = 0u;
}


/* ----------------------------------------------------------------------------- sliding door right */
static int16_t slide_right_pos;
static bcm_slide_t slide_right_state;
static uint8_t slide_right_obstacles;

bcm_slide_t bcm_slide_door_right(bool request, bool child_lock_inner, bool inner_request, bool locked, int16_t speed_kph,
                               bool fuel_lid_open)
{
    uint16_t current = hal_adc_read(BCM_ADC_SLIDE_R);
    bool want = request || (inner_request && !child_lock_inner);
    if (speed_kph > 0) {
        if (slide_right_state == BCM_SLIDE_OPENING || slide_right_state == BCM_SLIDE_CLOSING) {
            slide_right_state = BCM_SLIDE_STOPPED;
        }
    } else if ((slide_right_state == BCM_SLIDE_OPENING || slide_right_state == BCM_SLIDE_CLOSING) &&
               current > BCM_SLIDE_LIMIT_MA) {
        slide_right_state = slide_right_state == BCM_SLIDE_CLOSING ? BCM_SLIDE_OPENING : BCM_SLIDE_STOPPED;
        if (slide_right_obstacles < 255u) {
            slide_right_obstacles++;
        }
    } else if (slide_right_state == BCM_SLIDE_OPENING) {
        slide_right_pos = clamp_i16((int16_t)(slide_right_pos + 25), 0, BCM_SLIDE_OPEN_POS);
        if (want) {
            slide_right_state = BCM_SLIDE_STOPPED;
        } else if (slide_right_pos >= BCM_SLIDE_OPEN_POS) {
            slide_right_state = BCM_SLIDE_OPEN;
        }
    } else if (slide_right_state == BCM_SLIDE_CLOSING) {
        slide_right_pos = clamp_i16((int16_t)(slide_right_pos - 25), 0, BCM_SLIDE_OPEN_POS);
        if (want) {
            slide_right_state = BCM_SLIDE_STOPPED;
        } else if (slide_right_pos <= 0) {
            slide_right_state = BCM_SLIDE_CLOSED;
        }
    } else if (want && (slide_right_state == BCM_SLIDE_CLOSED || slide_right_state == BCM_SLIDE_STOPPED) && !locked) {
        bool blocked = fuel_lid_open && slide_right_pos < BCM_SLIDE_OPEN_POS / 4;
        if (!blocked) {
            slide_right_state = BCM_SLIDE_OPENING;
        }
    } else if (want && slide_right_state == BCM_SLIDE_OPEN) {
        slide_right_state = BCM_SLIDE_CLOSING;
    }
    hal_gpio_write(BCM_PIN_SLIDE_R_OPEN, slide_right_state == BCM_SLIDE_OPENING);
    hal_gpio_write(BCM_PIN_SLIDE_R_CLOSE, slide_right_state == BCM_SLIDE_CLOSING);
    return slide_right_state;
}

int16_t bcm_slide_door_right_position(void)
{
    return slide_right_pos;
}

uint8_t bcm_slide_door_right_obstacles(void)
{
    return slide_right_obstacles;
}

void bcm_slide_door_right_reset(void)
{
    slide_right_pos = 0;
    slide_right_state = BCM_SLIDE_CLOSED;
    slide_right_obstacles = 0u;
}


/* ----------------------------------------------------------------------------- steering wheel heater */
static uint32_t g_wheel_heat_ms;
static bool g_wheel_heat;

bool bcm_wheel_heater(bool button, int16_t wheel_temp, bcm_power_t power, uint8_t shed_level, uint16_t dt_ms)
{
    if (power != BCM_POWER_RUN || !bcm_load_allowed(BCM_LOAD_HEATING, shed_level)) {
        g_wheel_heat = false;
        g_wheel_heat_ms = 0u;
        return false;
    }
    if (button) {
        g_wheel_heat = !g_wheel_heat;
        g_wheel_heat_ms = 0u;
    }
    if (g_wheel_heat) {
        g_wheel_heat_ms += dt_ms;
        if (g_wheel_heat_ms > BCM_WHEEL_HEAT_MAX_MS || wheel_temp > BCM_WHEEL_HEAT_TEMP) {
            g_wheel_heat = false;
        }
    }
    return g_wheel_heat;
}

/* --------------------------------------------------------------------------------- headlamp leveling */
static int16_t g_level_now;

int16_t bcm_headlamp_level(int16_t pitch, int16_t speed_kph, bool static_mode)
{
    int16_t target = lut_headlamp_level_lookup(clamp_i16(pitch, -20, 20));
    int16_t step = static_mode || speed_kph < 5 ? 10 : 1;
    if (target > g_level_now) {
        g_level_now = (int16_t)(g_level_now + (target - g_level_now > step ? step : target - g_level_now));
    } else if (target < g_level_now) {
        g_level_now = (int16_t)(g_level_now - (g_level_now - target > step ? step : g_level_now - target));
    }
    return g_level_now;
}

/* ---------------------------------------------------------------------------------- cornering lights */
uint8_t bcm_cornering(int16_t steering_deg, bcm_turn_t turn, bool low_beam, int16_t speed_kph, bool reverse)
{
    uint8_t out = 0u;
    if (!low_beam || speed_kph > BCM_CORNER_MAX_KPH) {
        return 0u;
    }
    if (reverse) {
        return 3u;
    }
    if (steering_deg > BCM_CORNER_ANGLE || turn == BCM_TURN_LEFT) {
        out |= 1u;
    }
    if (steering_deg < -BCM_CORNER_ANGLE || turn == BCM_TURN_RIGHT) {
        out |= 2u;
    }
    return out;
}

void bcm_lamps_reset(void)
{
    g_wheel_heat_ms = 0u;
    g_wheel_heat = false;
    g_level_now = 0;
}
