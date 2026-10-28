/* climate_ctrl.c - automatic climate control (requirements in docs/requirements.md).
   One translation unit with the module's state machines, signal conditioning, fault handling, CAN layer and
   stored parameters. Limits only this file needs are macros of this file; the state is file-local. */
#include "climate.h"
#include "clim_hal.h"

#include <string.h>

/* ------------------------------------------------------------------------------------------ limits */
#define CLIM_RAW_MIN 50u
#define CLIM_RAW_MAX 4000u
#define CLIM_SETPOINT_MIN_DC 160
#define CLIM_SETPOINT_MAX_DC 300
#define CLIM_SETPOINT_DEFAULT_DC 220
#define CLIM_FAULT_CONFIRM 3u
#define CLIM_FAULT_HEAL 5u
#define CLIM_DEFROST_MAX_MS 600000u
#define CLIM_ECO_MAX_MS 1800000u
#define CLIM_COMP_MIN_OFF_MS 120000u
#define CLIM_COMP_START_MS 3000u
#define CLIM_COMP_LOCK_COUNT 3u
#define CLIM_AC_MIN_OUTSIDE_DC 20
#define CLIM_PRESSURE_HIGH_KPA 3000u
#define CLIM_PRESSURE_LOW_KPA 150u
#define CLIM_EVAP_FREEZE_DC 20
#define CLIM_RECIRC_PM_HIGH 120u
#define CLIM_RECIRC_PM_LOW 60u
#define CLIM_RECIRC_MAX_MS 900000u
#define CLIM_REAR_DEFROST_MS 900000u
#define CLIM_REAR_MIN_SUPPLY_MV 11500u
#define CLIM_PARAM_BLOCK 4u
#define CLIM_PARAM_VERSION 2u
#define CLIM_ECO_FAN_MAX_PCT 60u

/* ------------------------------------------------------------------------------------------ state */
typedef struct {
    uint8_t counter;      /* failed reports in a row (confirm) or passed reports in a row (heal) */
    bool active;
    bool confirmed;
} clim_fault_entry_t;

typedef struct {
    int16_t setpoint_dc;
    uint8_t mix_pct;
} clim_zone_state_t;

typedef struct {
    int16_t default_setpoint_dc;
    uint8_t eco_fan_max_pct;
    uint8_t version;
    uint8_t reserved;
    uint8_t crc;
} clim_params_t;

static clim_fault_entry_t s_faults[CLIM_FAULT_COUNT];
static clim_fault_cb_t s_fault_cb;
static clim_zone_state_t s_zones[CLIM_ZONE_COUNT];
static clim_params_t s_params;
static clim_mode_t s_mode;
static clim_mode_t s_mode_before_defrost;
static uint32_t s_defrost_ms;
static uint32_t s_eco_ms;
static clim_comp_state_t s_comp;
static uint32_t s_comp_off_ms;
static uint32_t s_comp_start_ms;
static uint8_t s_comp_protections;
static bool s_sync;
static bool s_rear_enabled;
static bool s_recirc;
static bool s_recirc_auto;
static uint32_t s_recirc_ms;
static bool s_rear_defrost;
static bool s_rear_button_last;
static uint32_t s_rear_defrost_ms;
static uint8_t s_last_alive;
static bool s_alive_valid;
static uint8_t s_tx_alive;

/* ------------------------------------------------------------------------------------------ helpers */
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

static uint8_t clamp_pct(int32_t x)
{
    if (x < 0) {
        return 0u;
    }
    if (x > 100) {
        return 100u;
    }
    return (uint8_t)x;
}

static uint32_t add_ms(uint32_t total, uint16_t dt_ms, uint32_t limit)
{
    uint32_t sum = total + dt_ms;
    return sum > limit ? limit : sum;
}

/* ------------------------------------------------------------------------------------------ signals */
int16_t clim_interp(const int16_t *xs, const int16_t *ys, uint8_t n, int16_t x)
{
    uint8_t i;

    if (xs == NULL || ys == NULL || n == 0u) {
        return 0;
    }
    if (n == 1u || x <= xs[0]) {
        return ys[0];
    }
    if (x >= xs[n - 1u]) {
        return ys[n - 1u];
    }
    for (i = 1u; i < n - 1u && x > xs[i]; i++) {
    }
    /* xs[i - 1] < x <= xs[i]: the segment of x */
    return (int16_t)(ys[i - 1u] + ((int32_t)ys[i] - ys[i - 1u]) * ((int32_t)x - xs[i - 1u]) /
                     ((int32_t)xs[i] - xs[i - 1u]));
}

uint8_t clim_crc8(const uint8_t *data, uint8_t len)
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

int16_t clim_filter_temp(int16_t previous_dc, int16_t sample_dc, uint8_t alpha_pct)
{
    int32_t delta;

    if (alpha_pct >= 100u) {
        return sample_dc;
    }
    if (alpha_pct == 0u) {
        return previous_dc;
    }
    delta = (int32_t)sample_dc - previous_dc;
    return (int16_t)(previous_dc + (delta * alpha_pct) / 100);
}

bool clim_raw_valid(uint16_t raw)
{
    return raw >= CLIM_RAW_MIN && raw <= CLIM_RAW_MAX;
}

int16_t clim_raw_to_dc(uint16_t raw)
{
    static const int16_t raw_points[] = {50, 500, 1500, 2500, 3500, 4000};
    static const int16_t dc_points[] = {-400, -150, 100, 300, 600, 850};

    if (!clim_raw_valid(raw)) {
        return 0;
    }
    return clim_interp(raw_points, dc_points, (uint8_t)(sizeof raw_points / sizeof raw_points[0]), (int16_t)raw);
}

bool clim_read_inputs(clim_inputs_t *in)
{
    uint8_t zone;
    uint16_t raw;
    bool ok = true;

    if (in == NULL) {
        return false;
    }
    for (zone = 0u; zone < (uint8_t)CLIM_ZONE_COUNT; zone++) {
        raw = hal_adc_read((uint8_t)(CLIM_ADC_CABIN_BASE + zone));
        if (clim_raw_valid(raw)) {
            in->cabin_dc[zone] = clim_raw_to_dc(raw);
        } else {
            ok = false;
        }
    }
    raw = hal_adc_read(CLIM_ADC_OUTSIDE);
    if (clim_raw_valid(raw)) {
        in->outside_dc = clim_raw_to_dc(raw);
    } else {
        ok = false;
    }
    raw = hal_adc_read(CLIM_ADC_EVAP);
    if (clim_raw_valid(raw)) {
        in->evap_dc = clim_raw_to_dc(raw);
    } else {
        ok = false;
    }
    in->pressure_kpa = (uint16_t)(hal_adc_read(CLIM_ADC_PRESSURE) * 2u);
    raw = hal_adc_read(CLIM_ADC_SOLAR);
    in->solar_pct = (uint8_t)(raw > 4000u ? 100u : raw / 40u);
    clim_fault_report(CLIM_FAULT_SENSOR, !ok);
    return ok;
}

/* ------------------------------------------------------------------------------------------ faults */
void clim_fault_register_cb(clim_fault_cb_t cb)
{
    s_fault_cb = cb;
}

void clim_fault_report(clim_fault_t fault, bool failed)
{
    clim_fault_entry_t *entry;
    bool was_active;

    if (fault <= CLIM_FAULT_NONE || fault >= CLIM_FAULT_COUNT) {
        return;
    }
    entry = &s_faults[fault];
    was_active = entry->active;
    if (failed) {
        if (entry->active) {
            entry->counter = 0u;
        } else if (++entry->counter >= CLIM_FAULT_CONFIRM) {
            entry->active = true;
            entry->confirmed = true;
            entry->counter = 0u;
        }
    } else {
        if (!entry->active) {
            entry->counter = 0u;
        } else if (++entry->counter >= CLIM_FAULT_HEAL) {
            entry->active = false;
            entry->counter = 0u;
        }
    }
    if (entry->active != was_active && s_fault_cb != NULL) {
        s_fault_cb(fault, entry->active);
    }
}

bool clim_fault_active(clim_fault_t fault)
{
    if (fault <= CLIM_FAULT_NONE || fault >= CLIM_FAULT_COUNT) {
        return false;
    }
    return s_faults[fault].active;
}

uint8_t clim_fault_count_confirmed(void)
{
    uint8_t count = 0u;
    uint8_t f;

    for (f = (uint8_t)CLIM_FAULT_SENSOR; f < (uint8_t)CLIM_FAULT_COUNT; f++) {
        if (s_faults[f].confirmed) {
            count++;
        }
    }
    return count;
}

void clim_diag_flags_update(void)
{
    uint8_t f;

    for (f = (uint8_t)CLIM_FAULT_SENSOR; f < (uint8_t)CLIM_FAULT_COUNT; f++) {
        if (s_faults[f].active) {
            CLIM_DIAG_REG |= (1u << f);
        } else {
            CLIM_DIAG_REG &= ~(1u << f);
        }
    }
}

bool clim_hw_ready(void)
{
    return (CLIM_STATUS_REG & CLIM_STATUS_READY) != 0u;
}

/* ------------------------------------------------------------------------------------------ modes */
clim_mode_t clim_mode_step(clim_cmd_t cmd, bool engine_running, uint16_t dt_ms)
{
    switch (cmd) {
    case CLIM_CMD_ON:
        if (s_mode == CLIM_MODE_OFF) {
            s_mode = CLIM_MODE_AUTO;
        }
        break;
    case CLIM_CMD_OFF:
        s_mode = CLIM_MODE_OFF;
        break;
    case CLIM_CMD_DEFROST:
        if (s_mode != CLIM_MODE_DEFROST) {
            s_mode_before_defrost = s_mode;
            s_mode = CLIM_MODE_DEFROST;
            s_defrost_ms = 0u;
        } else {
            s_mode = s_mode_before_defrost;
        }
        break;
    case CLIM_CMD_ECO:
        if (s_mode == CLIM_MODE_AUTO || s_mode == CLIM_MODE_MANUAL) {
            s_mode = CLIM_MODE_ECO;
            s_eco_ms = 0u;
        }
        break;
    case CLIM_CMD_MANUAL:
        if (s_mode != CLIM_MODE_OFF) {
            s_mode = CLIM_MODE_MANUAL;
        }
        break;
    case CLIM_CMD_NONE:
    default:
        break;
    }
    if (s_mode == CLIM_MODE_DEFROST) {
        s_defrost_ms = add_ms(s_defrost_ms, dt_ms, CLIM_DEFROST_MAX_MS);
        if (s_defrost_ms >= CLIM_DEFROST_MAX_MS) {
            s_mode = s_mode_before_defrost == CLIM_MODE_OFF ? CLIM_MODE_AUTO : s_mode_before_defrost;
        }
    } else if (s_mode == CLIM_MODE_ECO) {
        s_eco_ms = add_ms(s_eco_ms, dt_ms, CLIM_ECO_MAX_MS);
        if (s_eco_ms >= CLIM_ECO_MAX_MS && engine_running) {
            s_mode = CLIM_MODE_AUTO;
        }
    }
    if (!engine_running && s_mode == CLIM_MODE_MANUAL) {
        s_mode = CLIM_MODE_AUTO;
    }
    return s_mode;
}

clim_mode_t clim_mode(void)
{
    return s_mode;
}

/* ------------------------------------------------------------------------------------------ compressor */
clim_comp_state_t clim_compressor_step(bool request, int16_t outside_dc, uint16_t pressure_kpa, int16_t evap_dc,
                                       uint16_t dt_ms)
{
    bool pressure_bad = pressure_kpa > CLIM_PRESSURE_HIGH_KPA || pressure_kpa < CLIM_PRESSURE_LOW_KPA;

    switch (s_comp) {
    case CLIM_COMP_OFF:
        s_comp_off_ms = add_ms(s_comp_off_ms, dt_ms, CLIM_COMP_MIN_OFF_MS);
        if (request && outside_dc > CLIM_AC_MIN_OUTSIDE_DC && !pressure_bad && s_comp_off_ms >= CLIM_COMP_MIN_OFF_MS) {
            s_comp = CLIM_COMP_STARTING;
            s_comp_start_ms = 0u;
        }
        break;
    case CLIM_COMP_STARTING:
        s_comp_start_ms = add_ms(s_comp_start_ms, dt_ms, CLIM_COMP_START_MS);
        if (!request) {
            s_comp = CLIM_COMP_OFF;
            s_comp_off_ms = 0u;
        } else if (pressure_bad) {
            s_comp = CLIM_COMP_PROTECT;
        } else if (s_comp_start_ms >= CLIM_COMP_START_MS) {
            s_comp = CLIM_COMP_RUNNING;
        }
        break;
    case CLIM_COMP_RUNNING:
        if (pressure_bad || evap_dc < CLIM_EVAP_FREEZE_DC) {
            s_comp = CLIM_COMP_PROTECT;
        } else if (!request || outside_dc <= CLIM_AC_MIN_OUTSIDE_DC) {
            s_comp = CLIM_COMP_OFF;
            s_comp_off_ms = 0u;
        }
        break;
    case CLIM_COMP_PROTECT:
        s_comp_protections++;
        clim_fault_report(CLIM_FAULT_PRESSURE, pressure_bad);
        if (s_comp_protections >= CLIM_COMP_LOCK_COUNT) {
            s_comp = CLIM_COMP_LOCKED;
            clim_fault_report(CLIM_FAULT_COMPRESSOR, true);
        } else {
            s_comp = CLIM_COMP_OFF;
            s_comp_off_ms = 0u;
        }
        break;
    case CLIM_COMP_LOCKED:
    default:
        break;
    }
    hal_gpio_write(CLIM_PIN_COMPRESSOR, s_comp == CLIM_COMP_STARTING || s_comp == CLIM_COMP_RUNNING);
    return s_comp;
}

clim_comp_state_t clim_comp_state(void)
{
    return s_comp;
}

/* ------------------------------------------------------------------------------------------ blower and mix */
uint8_t clim_blower_pct(clim_mode_t mode, uint8_t fan_level, int16_t error_dc, bool engine_running)
{
    static const int16_t err_points[] = {0, 20, 50, 100, 200};
    static const int16_t pct_points[] = {20, 30, 45, 70, 100};
    int16_t magnitude;
    uint8_t pct;

    if (mode == CLIM_MODE_OFF) {
        return 0u;
    }
    if (mode == CLIM_MODE_DEFROST) {
        return 100u;
    }
    if (mode == CLIM_MODE_MANUAL && fan_level > 0u) {
        pct = clamp_pct((int32_t)fan_level * 100 / 7);
    } else {
        magnitude = (int16_t)(error_dc < 0 ? -error_dc : error_dc);
        pct = (uint8_t)clim_interp(err_points, pct_points, (uint8_t)(sizeof err_points / sizeof err_points[0]),
                                   magnitude);
    }
    if (mode == CLIM_MODE_ECO && pct > s_params.eco_fan_max_pct) {
        pct = s_params.eco_fan_max_pct;
    }
    if (!engine_running && pct > 40u) {
        pct = 40u;
    }
    return pct;
}

uint8_t clim_mix_pct(clim_zone_t zone, int16_t setpoint_dc, int16_t cabin_dc, uint8_t solar_pct)
{
    int32_t error;
    int32_t pct;

    if (zone >= CLIM_ZONE_COUNT) {
        return 0u;
    }
    if ((zone == CLIM_ZONE_REAR_LEFT || zone == CLIM_ZONE_REAR_RIGHT) && !s_rear_enabled) {
        return s_zones[CLIM_ZONE_DRIVER].mix_pct;
    }
    error = (int32_t)setpoint_dc - cabin_dc;
    pct = 50 + error / 2;
    if (solar_pct > 50u && (zone == CLIM_ZONE_DRIVER || zone == CLIM_ZONE_PASSENGER)) {
        pct -= (int32_t)(solar_pct - 50u) / 5;
    }
    s_zones[zone].mix_pct = clamp_pct(pct);
    return s_zones[zone].mix_pct;
}

/* ------------------------------------------------------------------------------------------ zones */
bool clim_zone_set(clim_zone_t zone, int16_t setpoint_dc)
{
    uint8_t z;

    if (zone >= CLIM_ZONE_COUNT) {
        return false;
    }
    if (setpoint_dc < CLIM_SETPOINT_MIN_DC || setpoint_dc > CLIM_SETPOINT_MAX_DC) {
        return false;
    }
    if (zone != CLIM_ZONE_DRIVER && s_sync) {
        return false;                           /* synchronised: only the driver sets the temperature */
    }
    s_zones[zone].setpoint_dc = setpoint_dc;
    if (zone == CLIM_ZONE_DRIVER && s_sync) {
        for (z = (uint8_t)CLIM_ZONE_PASSENGER; z < (uint8_t)CLIM_ZONE_COUNT; z++) {
            s_zones[z].setpoint_dc = setpoint_dc;
        }
    }
    return true;
}

int16_t clim_zone_setpoint(clim_zone_t zone)
{
    if (zone >= CLIM_ZONE_COUNT) {
        return CLIM_SETPOINT_DEFAULT_DC;
    }
    return s_zones[zone].setpoint_dc;
}

void clim_sync(bool on)
{
    uint8_t z;

    s_sync = on;
    if (on) {
        for (z = (uint8_t)CLIM_ZONE_PASSENGER; z < (uint8_t)CLIM_ZONE_COUNT; z++) {
            s_zones[z].setpoint_dc = s_zones[CLIM_ZONE_DRIVER].setpoint_dc;
        }
    }
}

void clim_rear_enable(bool on)
{
    s_rear_enabled = on;
}

/* ------------------------------------------------------------------------------------------ recirculation */
bool clim_recirc_step(bool request, bool defrost, uint16_t pm25, uint16_t dt_ms)
{
    if (defrost) {
        s_recirc = false;                       /* fresh air clears the windscreen */
        s_recirc_auto = false;
        s_recirc_ms = 0u;
    } else if (request) {
        s_recirc = true;
        s_recirc_auto = false;
    } else if (pm25 > CLIM_RECIRC_PM_HIGH && !s_recirc) {
        s_recirc = true;
        s_recirc_auto = true;
        s_recirc_ms = 0u;
    } else if (s_recirc_auto && pm25 < CLIM_RECIRC_PM_LOW) {
        s_recirc = false;
        s_recirc_auto = false;
    } else if (!s_recirc_auto) {
        s_recirc = false;
    }
    if (s_recirc) {
        s_recirc_ms = add_ms(s_recirc_ms, dt_ms, CLIM_RECIRC_MAX_MS);
        if (s_recirc_ms >= CLIM_RECIRC_MAX_MS) {   /* stale air: fresh air once, the timer restarts */
            s_recirc = false;
            s_recirc_auto = false;
            s_recirc_ms = 0u;
        }
    }
    hal_gpio_write(CLIM_PIN_RECIRC, s_recirc);
    return s_recirc;
}

/* ------------------------------------------------------------------------------------------ rear defrost */
bool clim_rear_defrost(bool button, uint16_t supply_mv, uint16_t dt_ms)
{
    bool pressed = button && !s_rear_button_last;

    s_rear_button_last = button;
    if (pressed) {
        s_rear_defrost = !s_rear_defrost;
        s_rear_defrost_ms = 0u;
    }
    if (s_rear_defrost) {
        if (supply_mv < CLIM_REAR_MIN_SUPPLY_MV) {
            s_rear_defrost = false;
        } else {
            s_rear_defrost_ms = add_ms(s_rear_defrost_ms, dt_ms, CLIM_REAR_DEFROST_MS);
            if (s_rear_defrost_ms >= CLIM_REAR_DEFROST_MS) {
                s_rear_defrost = false;
            }
        }
    }
    hal_gpio_write(CLIM_PIN_REAR_DEFROST, s_rear_defrost);
    return s_rear_defrost;
}

/* ------------------------------------------------------------------------------------------ CAN */
clim_can_result_t clim_can_parse(const uint8_t *frame, uint8_t len, clim_request_t *req)
{
    uint8_t alive;
    int16_t setpoint;
    uint8_t fan;

    if (frame == NULL || req == NULL || len != 8u) {
        return CLIM_CAN_ERR_LEN;
    }
    if (clim_crc8(frame, 7u) != frame[7]) {
        return CLIM_CAN_ERR_CRC;
    }
    alive = (uint8_t)(frame[6] & 0x0Fu);
    if (s_alive_valid && alive == s_last_alive) {
        return CLIM_CAN_ERR_ALIVE;
    }
    s_last_alive = alive;
    s_alive_valid = true;
    setpoint = (int16_t)((uint16_t)frame[0] | ((uint16_t)frame[1] << 8));
    fan = frame[2];
    if (setpoint < CLIM_SETPOINT_MIN_DC || setpoint > CLIM_SETPOINT_MAX_DC || fan > 7u) {
        return CLIM_CAN_ERR_RANGE;
    }
    req->setpoint_dc = setpoint;
    req->fan_level = fan;
    req->recirc = (frame[3] & 0x01u) != 0u;
    req->ac_request = (frame[3] & 0x02u) != 0u;
    return CLIM_CAN_OK;
}

bool clim_can_encode(const clim_outputs_t *out, uint8_t *frame, uint8_t len)
{
    if (out == NULL || frame == NULL || len < 8u) {
        return false;
    }
    memset(frame, 0, len);
    frame[0] = out->blower_pct;
    frame[1] = out->mix_pct[CLIM_ZONE_DRIVER];
    frame[2] = out->mix_pct[CLIM_ZONE_PASSENGER];
    frame[3] = (uint8_t)((out->compressor ? 0x01u : 0u) | (out->recirc ? 0x02u : 0u) |
                         (out->rear_defrost ? 0x04u : 0u));
    frame[4] = (uint8_t)s_mode;
    frame[5] = (uint8_t)s_comp;
    s_tx_alive = (uint8_t)((s_tx_alive + 1u) & 0x0Fu);
    frame[6] = s_tx_alive;
    frame[7] = clim_crc8(frame, 7u);
    return true;
}

bool clim_can_send_status(const clim_outputs_t *out)
{
    uint8_t frame[8];

    if (!clim_can_encode(out, frame, (uint8_t)sizeof frame)) {
        return false;
    }
    return can_send(CLIM_CAN_ID_STATUS, frame, (uint8_t)sizeof frame);
}

/* ------------------------------------------------------------------------------------------ parameters */
static void params_defaults(void)
{
    s_params.default_setpoint_dc = CLIM_SETPOINT_DEFAULT_DC;
    s_params.eco_fan_max_pct = CLIM_ECO_FAN_MAX_PCT;
    s_params.version = CLIM_PARAM_VERSION;
    s_params.reserved = 0u;
    s_params.crc = 0u;
}

bool clim_params_load(void)
{
    uint8_t buffer[sizeof(clim_params_t)];
    clim_params_t loaded;

    memset(buffer, 0, sizeof buffer);
    if (!nvm_read(CLIM_PARAM_BLOCK, buffer, (uint16_t)sizeof buffer)) {
        params_defaults();
        return false;
    }
    memcpy(&loaded, buffer, sizeof loaded);
    if (loaded.version != CLIM_PARAM_VERSION ||
        clim_crc8(buffer, (uint8_t)(sizeof buffer - 1u)) != buffer[sizeof buffer - 1u]) {
        params_defaults();
        return false;
    }
    if (loaded.default_setpoint_dc < CLIM_SETPOINT_MIN_DC || loaded.default_setpoint_dc > CLIM_SETPOINT_MAX_DC ||
        loaded.eco_fan_max_pct > 100u) {
        params_defaults();
        return false;
    }
    s_params = loaded;
    return true;
}

bool clim_params_save(void)
{
    uint8_t buffer[sizeof(clim_params_t)];

    s_params.version = CLIM_PARAM_VERSION;
    memcpy(buffer, &s_params, sizeof buffer);
    buffer[sizeof buffer - 1u] = clim_crc8(buffer, (uint8_t)(sizeof buffer - 1u));
    s_params.crc = buffer[sizeof buffer - 1u];
    return nvm_write(CLIM_PARAM_BLOCK, buffer, (uint16_t)sizeof buffer);
}

int16_t clim_default_setpoint(void)
{
    return s_params.default_setpoint_dc;
}

/* ------------------------------------------------------------------------------------------ the cycle */
void clim_init(void)
{
    uint8_t z;

    memset(s_faults, 0, sizeof s_faults);
    s_mode = CLIM_MODE_OFF;
    s_mode_before_defrost = CLIM_MODE_OFF;
    s_comp = CLIM_COMP_OFF;
    s_comp_off_ms = CLIM_COMP_MIN_OFF_MS;
    s_comp_protections = 0u;
    s_sync = true;
    s_rear_enabled = false;
    s_recirc = false;
    s_recirc_auto = false;
    s_rear_defrost = false;
    s_alive_valid = false;
    (void)clim_params_load();
    for (z = 0u; z < (uint8_t)CLIM_ZONE_COUNT; z++) {
        s_zones[z].setpoint_dc = s_params.default_setpoint_dc;
        s_zones[z].mix_pct = 50u;
    }
}

void clim_step(const clim_inputs_t *in, const clim_request_t *req, clim_outputs_t *out, uint16_t dt_ms)
{
    uint8_t z;
    int16_t error;
    bool defrost;

    if (in == NULL || req == NULL || out == NULL) {
        return;
    }
    memset(out, 0, sizeof *out);
    if (!clim_hw_ready()) {
        clim_fault_report(CLIM_FAULT_BLOWER, true);
        return;
    }
    clim_fault_report(CLIM_FAULT_BLOWER, false);
    if (s_mode == CLIM_MODE_OFF) {
        hal_pwm_set(CLIM_PWM_BLOWER, 0u);
        return;
    }
    defrost = s_mode == CLIM_MODE_DEFROST;
    error = clamp_i16((int16_t)(clim_zone_setpoint(CLIM_ZONE_DRIVER) - in->cabin_dc[CLIM_ZONE_DRIVER]), -500, 500);
    out->blower_pct = clim_blower_pct(s_mode, req->fan_level, error, true);
    for (z = 0u; z < (uint8_t)CLIM_ZONE_COUNT; z++) {
        out->mix_pct[z] = clim_mix_pct((clim_zone_t)z, clim_zone_setpoint((clim_zone_t)z), in->cabin_dc[z],
                                       in->solar_pct);
        hal_pwm_set((uint8_t)(CLIM_PWM_MIX_BASE + z), out->mix_pct[z]);
    }
    out->compressor = clim_compressor_step(req->ac_request || defrost, in->outside_dc, in->pressure_kpa, in->evap_dc,
                                           dt_ms) == CLIM_COMP_RUNNING;
    out->recirc = clim_recirc_step(req->recirc, defrost, in->pm25, dt_ms);
    out->rear_defrost = s_rear_defrost;
    hal_pwm_set(CLIM_PWM_BLOWER, out->blower_pct);
    clim_diag_flags_update();
}
