/* bcm.h - interface of the body control module (body_ctrl.c). */
#ifndef BCM_H
#define BCM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* timing and thresholds */
#define BCM_CRANK_MAX_MS 4000u
#define BCM_LUX_ON 1000
#define BCM_LUX_OFF 3000
#define BCM_LIGHT_ON_DELAY_MS 2000u
#define BCM_LIGHT_OFF_DELAY_MS 5000u
#define BCM_FOLLOW_ME_MS 30000u
#define BCM_BULB_LEAK_MA 50u
#define BCM_COMFORT_FLASHES 3u
#define BCM_FLASH_MS 660u
#define BCM_FLASH_FAST_MS 330u
#define BCM_RAIN_LIGHT 4
#define BCM_RAIN_HEAVY 10
#define BCM_WIPER_LOW_DUTY 50u
#define BCM_WIPER_HIGH_DUTY 100u
#define BCM_WIPER_PARK_DUTY 30u
#define BCM_AUTOLOCK_KPH 15
#define BCM_LOCK_WINDOW_MS 30000u
#define BCM_LOCK_MAX_CYCLES 10u
#define BCM_WIN_PINCH_ZONE 40
#define BCM_WIN_REVERSE_MS 300u
#define BCM_WIN_MAX_PINCHES 3u
#define BCM_SEAT_BOOST_MS 600000u
#define BCM_SEAT_MAX_MS 3600000u
#define BCM_SEAT_OVERTEMP 600
#define BCM_SHED1_MV 12200
#define BCM_SHED2_MV 11800
#define BCM_SHED3_MV 11400
#define BCM_SHED_HYST_MV 300
#define BCM_COURTESY_MS 20000u
#define BCM_CHIRP_MS 50u
#define BCM_ALARM_HALF_MS 500u
#define BCM_MIRROR_MAX_KPH 50
#define BCM_MIRROR_HEAT_MS 600000u
#define BCM_MIRROR_COLD 30
#define BCM_DTC_FAIL_LIMIT 3
#define BCM_DTC_PASS_LIMIT 3
#define BCM_DTC_AGING_CYCLES 40u
#define BCM_PARAMS_VERSION 3u
#define BCM_NVM_BLOCK_PARAMS 2u
#define BCM_FOB_WINDOW 256u
#define BCM_FOB_MAX_FAILURES 5u
#define BCM_FOB_BLOCK_MS 10000u
#define BCM_S3_TIMEOUT_MS 5000u
#define BCM_MAX_KEY_ATTEMPTS 3u
#define BCM_CLIMATE_DEADBAND 15
#define BCM_FAN_MIN_DUTY 10
#define BCM_AC_MIN_OUTSIDE 20
#define BCM_EVAP_FREEZE 10
#define BCM_AC_MAX_KPA 3000u
#define BCM_AC_MIN_KPA 200u
#define BCM_COMPRESSOR_LOCKOUT_MS 30000u
#define BCM_AIR_POLLUTED 70
#define BCM_RECIRC_MAX_MS 900000u
#define BCM_TPMS_TIMEOUT_MS 60000u
#define BCM_TPMS_SEVERE_KPA 150u
#define BCM_TPMS_LOW_KPA 180u
#define BCM_TPMS_HYST_KPA 20u
#define BCM_TPMS_OK 0u
#define BCM_TPMS_LOW 1u
#define BCM_TPMS_SEVERE 2u
#define BCM_TPMS_SENSOR_LOST 3u
#define BCM_ALARM_ARM_DELAY_MS 20000u
#define BCM_ALARM_MAX_TRIGGERS 10u
#define BCM_ALARM_SOUND_MS 30000u
#define BCM_GATE_OPEN_POS 1000
#define BCM_ROOF_OPEN 1000
#define BCM_ROOF_PINCH_MA 6000u
#define BCM_ROOF_PINCH_ZONE 40
#define BCM_ROOF_REVERSE_MS 400u
#define BCM_IMMO_MAX_FAILS 3u
#define BCM_PDC_BAD_LIMIT 3u
#define BCM_PDC_FAULT_CM 0xFFFFu
#define BCM_PDC_MAX_CM 150u
#define BCM_PDC_MAX_KPH 15
#define BCM_PDC_CONTINUOUS_CM 30u
#define BCM_FOG_MAX_KPH 120
#define BCM_EMERGENCY_DECEL 600
#define BCM_EMERGENCY_MIN_KPH 50
#define BCM_MAX_MM_PER_PULSE 5000u
#define BCM_ODO_MAX_M 999999999u
#define BCM_COLUMN_RETRIES 3u
#define BCM_LIN_RLS_ID 0x23u
#define BCM_LIN_MAX_ERRORS 3u
#define BCM_AMBIENT_COLOURS 6u
#define BCM_FUEL_OHM_FULL 10u
#define BCM_FUEL_OHM_EMPTY 180u
#define BCM_TANK_DL 600
#define BCM_FUEL_SLOSH_ACCEL 300
#define BCM_FUEL_LOW_DL 80
#define BCM_SEAT_SLOTS 3u
#define BCM_SEAT_STEP 8
#define BCM_HBA_MIN_KPH 40
#define BCM_HBA_CLEAR_MS 2000u
#define BCM_NM_TIMEOUT_MS 2000u
#define BCM_NM_WAIT_SLEEP_MS 1500u
#define BCM_SUPERVISION_LIMIT 3u
#define BCM_WASHER_LOW_MS 10000u
#define BCM_SLIDE_LIMIT_MA 7000u
#define BCM_SLIDE_OPEN_POS 1000
#define BCM_WHEEL_HEAT_MAX_MS 1800000u
#define BCM_WHEEL_HEAT_TEMP 350
#define BCM_CORNER_MAX_KPH 40
#define BCM_CORNER_ANGLE 30

/* bulb check results */
#define BCM_BULB_OK 0u
#define BCM_BULB_OPEN 1u
#define BCM_BULB_SHORT 2u

/* negative response codes */
#define BCM_NRC_OK 0x00u
#define BCM_NRC_SUBFUNCTION 0x12u
#define BCM_NRC_FORMAT 0x13u
#define BCM_NRC_CONDITIONS 0x22u
#define BCM_NRC_SEQUENCE 0x24u
#define BCM_NRC_OUT_OF_RANGE 0x31u
#define BCM_NRC_SECURITY 0x33u
#define BCM_NRC_INVALID_KEY 0x35u
#define BCM_NRC_EXCEEDED 0x36u
#define BCM_NRC_SESSION 0x7Fu

typedef enum { BCM_POWER_OFF, BCM_POWER_ACC, BCM_POWER_RUN, BCM_POWER_CRANK } bcm_power_t;
typedef enum { BCM_KEY_OFF, BCM_KEY_ACC, BCM_KEY_RUN, BCM_KEY_START } bcm_key_t;
typedef enum { BCM_LIGHT_OFF, BCM_LIGHT_AUTO, BCM_LIGHT_ON } bcm_light_switch_t;
typedef enum { BCM_TURN_NONE, BCM_TURN_LEFT, BCM_TURN_RIGHT, BCM_TURN_HAZARD } bcm_turn_t;
typedef enum { BCM_WIPER_OFF, BCM_WIPER_INTERVAL, BCM_WIPER_LOW, BCM_WIPER_HIGH, BCM_WIPER_AUTO } bcm_wiper_mode_t;
typedef enum { BCM_LOCK_NONE, BCM_LOCK, BCM_UNLOCK } bcm_lock_cmd_t;
typedef enum { BCM_WIN_HOLD, BCM_WIN_UP, BCM_WIN_DOWN } bcm_win_cmd_t;
typedef enum { BCM_WIN_STOPPED, BCM_WIN_CLOSING, BCM_WIN_OPENING, BCM_WIN_REVERSING } bcm_win_state_t;
typedef enum { BCM_LOAD_COMFORT, BCM_LOAD_HEATING, BCM_LOAD_LIGHTING, BCM_LOAD_SAFETY } bcm_load_t;
typedef enum { BCM_HORN_NONE, BCM_HORN_LOCK_CHIRP, BCM_HORN_ALARM } bcm_horn_pattern_t;
typedef enum { BCM_ALARM_DISARMED, BCM_ALARM_ARMING, BCM_ALARM_ARMED, BCM_ALARM_TRIGGERED, BCM_ALARM_PANIC } bcm_alarm_t;
typedef enum { BCM_GATE_CLOSED, BCM_GATE_OPENING, BCM_GATE_OPEN, BCM_GATE_CLOSING, BCM_GATE_STOPPED } bcm_gate_t;
typedef enum { BCM_SLIDE_CLOSED, BCM_SLIDE_OPENING, BCM_SLIDE_OPEN, BCM_SLIDE_CLOSING, BCM_SLIDE_STOPPED } bcm_slide_t;
typedef enum { BCM_NM_SLEEP, BCM_NM_AWAKE, BCM_NM_PREPARE_SLEEP } bcm_nm_t;
typedef enum { BCM_SESSION_DEFAULT = 1, BCM_SESSION_PROGRAMMING = 2, BCM_SESSION_EXTENDED = 3 } bcm_session_t;
typedef enum {
    BCM_DTC_CRANK_TIMEOUT, BCM_DTC_LOCK_OVERUSE, BCM_DTC_WINDOW_PINCH, BCM_DTC_SEAT_OVERTEMP, BCM_DTC_CAN_CRC,
    BCM_DTC_CAN_TIMEOUT, BCM_DTC_NVM, BCM_DTC_SCHEDULER, BCM_DTC_COUNT
} bcm_dtc_t;

typedef struct {
    uint16_t id;
    uint8_t value;
} bcm_event_t;

typedef struct {
    uint32_t follow_me_ms;
    int16_t autolock_kph;
    int16_t lux_on;
    int16_t lux_off;
    uint8_t version;
    uint8_t auto_fold;
    uint8_t lock_chirp;
    uint8_t reserved;
    uint8_t crc;
} bcm_params_t;

typedef struct {
    bcm_key_t key;
    bool brake;
    bool engine_running;
    bool hazard;
    bool horn;
    bcm_turn_t turn_lever;
    bool turn_tap;
    bool crash;
    bool express;
    bcm_win_cmd_t win_fl;
    bcm_win_cmd_t win_fr;
    bcm_win_cmd_t win_rl;
    bcm_win_cmd_t win_rr;
    int32_t lux;
    bool auto_lights;
    bcm_light_switch_t light_switch;
    bcm_wiper_mode_t wiper;
    bcm_wiper_mode_t rear_wiper;
    bool washer;
    int16_t rain;
    int16_t speed_kph;
    uint8_t doors;
    bcm_lock_cmd_t lock;
    int32_t battery_mv;
    bool tester_present;
} bcm_inputs_t;

int16_t lut_wiper_interval_lookup(int16_t x);
int16_t lut_headlamp_level_lookup(int16_t x);
int16_t lut_seat_heat_target_lookup(int16_t x);
int16_t lut_fan_duty_lookup(int16_t x);
int16_t lut_dim_curve_lookup(int16_t x);
int16_t lut_bulb_current_lookup(int16_t x);
int32_t ema_battery_mv_update(int32_t sample);
void ema_battery_mv_reset(void);
int32_t ema_light_lux_update(int32_t sample);
void ema_light_lux_reset(void);
int16_t ema_cabin_temp_update(int16_t sample);
void ema_cabin_temp_reset(void);
int16_t ema_rain_level_update(int16_t sample);
void ema_rain_level_reset(void);
int16_t ema_vehicle_speed_update(int16_t sample);
void ema_vehicle_speed_reset(void);
int16_t bcm_q15_mul(int16_t a, int16_t b);
int16_t bcm_sat_add_i16(int16_t a, int16_t b);
uint16_t bcm_sat_sub_u16(uint16_t a, uint16_t b);
int32_t bcm_div_round(int32_t num, int32_t den);
int16_t bcm_clamp_signal(int16_t value, int16_t lo, int16_t hi);
uint16_t bcm_clamp_counter(uint16_t value, uint16_t lo, uint16_t hi);
int32_t bcm_clamp_wide(int32_t value, int32_t lo, int32_t hi);
bool sw_door_fl_debounce(bool raw, uint16_t dt_ms);
void sw_door_fl_debounce_reset(void);
bool sw_door_fr_debounce(bool raw, uint16_t dt_ms);
void sw_door_fr_debounce_reset(void);
bool sw_door_rl_debounce(bool raw, uint16_t dt_ms);
void sw_door_rl_debounce_reset(void);
bool sw_door_rr_debounce(bool raw, uint16_t dt_ms);
void sw_door_rr_debounce_reset(void);
bool sw_trunk_debounce(bool raw, uint16_t dt_ms);
void sw_trunk_debounce_reset(void);
bool sw_hood_debounce(bool raw, uint16_t dt_ms);
void sw_hood_debounce_reset(void);
bool sw_hazard_debounce(bool raw, uint16_t dt_ms);
void sw_hazard_debounce_reset(void);
bool sw_lock_debounce(bool raw, uint16_t dt_ms);
void sw_lock_debounce_reset(void);
bool sw_unlock_debounce(bool raw, uint16_t dt_ms);
void sw_unlock_debounce_reset(void);
bool sw_horn_debounce(bool raw, uint16_t dt_ms);
void sw_horn_debounce_reset(void);
bool sw_brake_debounce(bool raw, uint16_t dt_ms);
void sw_brake_debounce_reset(void);
bool sw_reverse_debounce(bool raw, uint16_t dt_ms);
void sw_reverse_debounce_reset(void);
bool sw_fog_front_debounce(bool raw, uint16_t dt_ms);
void sw_fog_front_debounce_reset(void);
bool sw_fog_rear_debounce(bool raw, uint16_t dt_ms);
void sw_fog_rear_debounce_reset(void);
bool sw_tailgate_debounce(bool raw, uint16_t dt_ms);
void sw_tailgate_debounce_reset(void);
bool sw_sunroof_open_debounce(bool raw, uint16_t dt_ms);
void sw_sunroof_open_debounce_reset(void);
bool sw_sunroof_close_debounce(bool raw, uint16_t dt_ms);
void sw_sunroof_close_debounce_reset(void);
bool sw_kick_debounce(bool raw, uint16_t dt_ms);
void sw_kick_debounce_reset(void);
bool sw_panic_debounce(bool raw, uint16_t dt_ms);
void sw_panic_debounce_reset(void);
bool sw_seatbelt_debounce(bool raw, uint16_t dt_ms);
void sw_seatbelt_debounce_reset(void);
bool sw_rear_defrost_debounce(bool raw, uint16_t dt_ms);
void sw_rear_defrost_debounce_reset(void);
bool sw_child_lock_debounce(bool raw, uint16_t dt_ms);
void sw_child_lock_debounce_reset(void);
bool sw_ac_debounce(bool raw, uint16_t dt_ms);
void sw_ac_debounce_reset(void);
bool sw_recirc_debounce(bool raw, uint16_t dt_ms);
void sw_recirc_debounce_reset(void);
bool sw_fuel_lid_debounce(bool raw, uint16_t dt_ms);
void sw_fuel_lid_debounce_reset(void);
bool sw_parking_brake_debounce(bool raw, uint16_t dt_ms);
void sw_parking_brake_debounce_reset(void);
bool sw_mirror_fold_debounce(bool raw, uint16_t dt_ms);
void sw_mirror_fold_debounce_reset(void);
bool sw_slide_l_debounce(bool raw, uint16_t dt_ms);
void sw_slide_l_debounce_reset(void);
bcm_power_t bcm_power_mode(void);
bcm_power_t bcm_power_step(bcm_key_t key, bool brake, bool engine_running, uint16_t dt_ms);
void bcm_power_reset(void);
bool bcm_lights_auto(int32_t lux, bool auto_switch, bool wipers_fast, bcm_power_t power, uint16_t dt_ms);
bool bcm_low_beam(bcm_light_switch_t sw, bool auto_on, bool driver_door_open, bcm_power_t power, uint16_t dt_ms);
uint8_t bcm_bulb_check(uint16_t supply_mv, uint16_t measured_ma, bool commanded);
void bcm_lights_reset(void);
bcm_turn_t bcm_turn_request(bcm_turn_t lever, bool hazard, bool tap, bool crash);
bool bcm_turn_flash(uint16_t dt_ms, bool bulb_failed);
void bcm_turn_reset(void);
uint8_t bcm_wiper_front(bcm_wiper_mode_t mode, int16_t speed_kph, int16_t rain_level, bool washer, uint16_t dt_ms);
bool bcm_wiper_front_parking(void);
void bcm_wiper_front_reset(void);
uint8_t bcm_wiper_rear(bcm_wiper_mode_t mode, int16_t speed_kph, int16_t rain_level, bool washer, uint16_t dt_ms);
bool bcm_wiper_rear_parking(void);
void bcm_wiper_rear_reset(void);
bool bcm_lock_command(bcm_lock_cmd_t cmd, int16_t speed_kph, bool crash, bool any_door_open, uint16_t dt_ms);
bool bcm_child_lock(bool request, bool rear_door_open);
bool bcm_inner_handle_opens(bool rear, bool locked);
void bcm_lock_reset(void);
bcm_win_state_t bcm_window_fl(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms);
int16_t bcm_window_fl_position(void);
void bcm_window_fl_set_position(int16_t pos);
void bcm_window_fl_reset(void);
bcm_win_state_t bcm_window_fr(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms);
int16_t bcm_window_fr_position(void);
void bcm_window_fr_set_position(int16_t pos);
void bcm_window_fr_reset(void);
bcm_win_state_t bcm_window_rl(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms);
int16_t bcm_window_rl_position(void);
void bcm_window_rl_set_position(int16_t pos);
void bcm_window_rl_reset(void);
bcm_win_state_t bcm_window_rr(bcm_win_cmd_t cmd, bool express, uint16_t dt_ms);
int16_t bcm_window_rr_position(void);
void bcm_window_rr_set_position(int16_t pos);
void bcm_window_rr_reset(void);
uint8_t bcm_seat_heater_driver(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms);
uint8_t bcm_seat_heater_driver_level(void);
void bcm_seat_heater_driver_reset(void);
uint8_t bcm_seat_heater_passenger(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms);
uint8_t bcm_seat_heater_passenger_level(void);
void bcm_seat_heater_passenger_reset(void);
uint8_t bcm_seat_heater_rear_left(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms);
uint8_t bcm_seat_heater_rear_left_level(void);
void bcm_seat_heater_rear_left_reset(void);
uint8_t bcm_seat_heater_rear_right(uint8_t request, bool occupied, bcm_power_t power, uint16_t dt_ms);
uint8_t bcm_seat_heater_rear_right_level(void);
void bcm_seat_heater_rear_right_reset(void);
uint8_t bcm_load_shed(int32_t battery_mv, bool engine_running);
bool bcm_load_allowed(bcm_load_t load, uint8_t shed_level);
void bcm_load_shed_reset(void);
uint8_t bcm_interior_light(bool any_door_open, bool switch_on, bool locked, uint16_t dt_ms);
void bcm_interior_light_reset(void);
bool bcm_horn(bool pressed, bcm_horn_pattern_t pattern, uint16_t dt_ms);
void bcm_horn_reset(void);
bool bcm_mirror_fold(bool fold_request, bool locked, int16_t speed_kph, bool auto_fold);
bool bcm_mirror_heat(bool rear_defrost, int16_t outside_temp, uint16_t dt_ms);
void bcm_mirror_reset(void);
int32_t sig_vehicle_speed_decode(const uint8_t *data);
bool sig_vehicle_speed_encode(uint8_t *data, int32_t physical);
int32_t sig_outside_temp_decode(const uint8_t *data);
bool sig_outside_temp_encode(uint8_t *data, int32_t physical);
int32_t sig_light_lux_decode(const uint8_t *data);
bool sig_light_lux_encode(uint8_t *data, int32_t physical);
int32_t sig_rain_level_decode(const uint8_t *data);
bool sig_rain_level_encode(uint8_t *data, int32_t physical);
int32_t sig_battery_mv_decode(const uint8_t *data);
bool sig_battery_mv_encode(uint8_t *data, int32_t physical);
int32_t sig_body_pitch_decode(const uint8_t *data);
bool sig_body_pitch_encode(uint8_t *data, int32_t physical);
int32_t sig_door_state_decode(const uint8_t *data);
bool sig_door_state_encode(uint8_t *data, int32_t physical);
int32_t sig_lock_req_decode(const uint8_t *data);
bool sig_lock_req_encode(uint8_t *data, int32_t physical);
int32_t sig_key_pos_decode(const uint8_t *data);
bool sig_key_pos_encode(uint8_t *data, int32_t physical);
int32_t sig_crash_decode(const uint8_t *data);
bool sig_crash_encode(uint8_t *data, int32_t physical);
uint8_t bcm_crc8_j1850(const uint8_t *data, uint8_t len);
uint32_t bcm_get_bits(const uint8_t *data, uint8_t start, uint8_t length);
bool bcm_set_bits(uint8_t *data, uint8_t start, uint8_t length, uint32_t value);
bool bcm_msg_vehicle_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_vehicle_tick(uint16_t dt_ms);
uint32_t bcm_msg_vehicle_id(void);
void bcm_msg_vehicle_reset(void);
bool bcm_msg_engine_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_engine_tick(uint16_t dt_ms);
uint32_t bcm_msg_engine_id(void);
void bcm_msg_engine_reset(void);
bool bcm_msg_brake_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_brake_tick(uint16_t dt_ms);
uint32_t bcm_msg_brake_id(void);
void bcm_msg_brake_reset(void);
bool bcm_msg_climate_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_climate_tick(uint16_t dt_ms);
uint32_t bcm_msg_climate_id(void);
void bcm_msg_climate_reset(void);
bool bcm_msg_door_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_door_tick(uint16_t dt_ms);
uint32_t bcm_msg_door_id(void);
void bcm_msg_door_reset(void);
bool bcm_msg_airbag_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_airbag_tick(uint16_t dt_ms);
uint32_t bcm_msg_airbag_id(void);
void bcm_msg_airbag_reset(void);
bool bcm_msg_tpms_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_tpms_tick(uint16_t dt_ms);
uint32_t bcm_msg_tpms_id(void);
void bcm_msg_tpms_reset(void);
bool bcm_msg_park_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_park_tick(uint16_t dt_ms);
uint32_t bcm_msg_park_id(void);
void bcm_msg_park_reset(void);
bool bcm_msg_gateway_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_gateway_tick(uint16_t dt_ms);
uint32_t bcm_msg_gateway_id(void);
void bcm_msg_gateway_reset(void);
bool bcm_msg_steering_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_steering_tick(uint16_t dt_ms);
uint32_t bcm_msg_steering_id(void);
void bcm_msg_steering_reset(void);
bool bcm_msg_wheel_speed_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_wheel_speed_tick(uint16_t dt_ms);
uint32_t bcm_msg_wheel_speed_id(void);
void bcm_msg_wheel_speed_reset(void);
bool bcm_msg_transmission_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_transmission_tick(uint16_t dt_ms);
uint32_t bcm_msg_transmission_id(void);
void bcm_msg_transmission_reset(void);
bool bcm_msg_infotainment_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_infotainment_tick(uint16_t dt_ms);
uint32_t bcm_msg_infotainment_id(void);
void bcm_msg_infotainment_reset(void);
bool bcm_msg_adas_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_adas_tick(uint16_t dt_ms);
uint32_t bcm_msg_adas_id(void);
void bcm_msg_adas_reset(void);
bool bcm_msg_lighting_req_receive(const uint8_t *data, uint8_t len);
bool bcm_msg_lighting_req_tick(uint16_t dt_ms);
uint32_t bcm_msg_lighting_req_id(void);
void bcm_msg_lighting_req_reset(void);
void bcm_dtc_report(bcm_dtc_t id, bool failed);
uint8_t bcm_dtc_status(bcm_dtc_t id);
uint8_t bcm_dtc_occurrences(bcm_dtc_t id);
void bcm_dtc_operation_cycle(void);
uint8_t bcm_dtc_count_confirmed(void);
void bcm_dtc_freeze(bool frozen);
void bcm_dtc_clear(void);
bool bcm_params_load(void);
bool bcm_params_set_autolock(int16_t kph);
bool bcm_params_set_follow_me(uint32_t ms);
bool bcm_params_store(bcm_power_t power);
const bcm_params_t *bcm_params(void);
void bcm_params_reset(void);
bool evq_input_push(bcm_event_t item);
bool evq_input_pop(bcm_event_t *item);
uint8_t evq_input_size(void);
uint16_t evq_input_drops(void);
void evq_input_clear(void);
bool evq_can_tx_push(uint32_t item);
bool evq_can_tx_pop(uint32_t *item);
uint8_t evq_can_tx_size(void);
uint16_t evq_can_tx_drops(void);
void evq_can_tx_clear(void);
bcm_lock_cmd_t bcm_fob_frame(const uint8_t *frame, uint8_t len, uint32_t fob_id, uint16_t dt_ms);
void bcm_fob_resync(uint32_t counter);
uint8_t bcm_diag_session(bcm_session_t requested, int16_t speed_kph);
void bcm_diag_tick(uint16_t dt_ms, bool tester_present);
uint8_t bcm_diag_request_seed(uint16_t random, uint16_t *seed);
uint8_t bcm_diag_send_key(uint16_t key);
uint8_t bcm_diag_read(uint16_t did, uint8_t *out, uint8_t size, uint8_t *len);
uint8_t bcm_diag_write(uint16_t did, const uint8_t *data, uint8_t len);
bcm_session_t bcm_diag_current_session(void);
void bcm_diag_reset(void);
uint16_t bcm_schedule(const bcm_inputs_t *in, uint32_t now_ms);
uint16_t bcm_schedule_overruns(void);
void bcm_schedule_reset(void);
uint8_t bcm_climate_fan(int16_t cabin_temp, int16_t set_temp, bool auto_mode, uint8_t manual_level);
bool bcm_climate_ac(bool request, int16_t outside_temp, int16_t evaporator_temp, uint16_t pressure_kpa, uint16_t dt_ms);
bool bcm_climate_recirc(bool button, bool defrost, int16_t air_quality, uint16_t dt_ms);
void bcm_climate_reset(void);
uint8_t bcm_tpms_fl(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms);
uint16_t bcm_tpms_fl_pressure(void);
void bcm_tpms_fl_reset(void);
uint8_t bcm_tpms_fr(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms);
uint16_t bcm_tpms_fr_pressure(void);
void bcm_tpms_fr_reset(void);
uint8_t bcm_tpms_rl(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms);
uint16_t bcm_tpms_rl_pressure(void);
void bcm_tpms_rl_reset(void);
uint8_t bcm_tpms_rr(uint16_t kpa, int16_t temp_c, bool received, uint16_t dt_ms);
uint16_t bcm_tpms_rr_pressure(void);
void bcm_tpms_rr_reset(void);
uint8_t bcm_tpms_worst(void);
bcm_alarm_t bcm_alarm_step(bcm_lock_cmd_t fob, bool any_door_open, bool intrusion, bool tilt, bool panic, uint16_t dt_ms);
void bcm_alarm_reset(void);
bcm_gate_t bcm_tailgate(bool button, bool kick, bool key_nearby, bool locked, bool obstacle, int16_t speed_kph);
int16_t bcm_tailgate_position(void);
void bcm_tailgate_reset(void);
int16_t bcm_sunroof(bool open_sw, bool close_sw, int16_t rain_level, bool locked, uint16_t motor_ma, uint16_t dt_ms);
void bcm_sunroof_set_position(int16_t pos);
void bcm_sunroof_reset(void);
uint32_t bcm_immo_challenge(uint32_t random);
bool bcm_immo_response(uint32_t response, uint32_t secret);
bool bcm_immo_released(void);
uint32_t bcm_immo_expected(uint32_t challenge, uint32_t secret);
void bcm_immo_reset(void);
uint16_t bcm_pdc_rear_left(uint16_t echo_us, bool echo_valid);
void bcm_pdc_rear_left_reset(void);
uint16_t bcm_pdc_rear_center(uint16_t echo_us, bool echo_valid);
void bcm_pdc_rear_center_reset(void);
uint16_t bcm_pdc_rear_right(uint16_t echo_us, bool echo_valid);
void bcm_pdc_rear_right_reset(void);
uint16_t bcm_pdc_front_center(uint16_t echo_us, bool echo_valid);
void bcm_pdc_front_center_reset(void);
uint16_t bcm_pdc_beep_interval(uint16_t nearest_cm, bool reverse, int16_t speed_kph);
bool bcm_drl(bool low_beam, bool parking_brake, bcm_power_t power, bool engine_running);
uint8_t bcm_fog_lamps(bool front_sw, bool rear_sw, bool low_beam, int16_t speed_kph);
bool bcm_brake_lamp(bool pedal, int16_t decel_cms2, int16_t speed_kph, uint16_t dt_ms);
bool bcm_reverse_lamp(bool reverse_gear, bcm_power_t power);
uint32_t bcm_odometer(uint16_t wheel_pulses, uint16_t mm_per_pulse, bool reset_trip);
uint32_t bcm_trip(void);
void bcm_odometer_reset(void);
bool bcm_column_lock(bool unlock_request, bool immo_ok, int16_t speed_kph, bool lock_sensor);
void bcm_column_reset(void);
bool bcm_lin_rls_frame(uint8_t pid, const uint8_t *data, uint8_t len);
int16_t bcm_lin_rain(void);
int32_t bcm_lin_lux(void);
uint8_t bcm_lin_pid(uint8_t id);
uint8_t bcm_lin_checksum(uint8_t pid, const uint8_t *data, uint8_t len);
void bcm_lin_reset(void);
uint32_t bcm_ambient_rgb(uint8_t colour, uint8_t brightness, bool night, bool alarm);
int32_t bcm_fuel_level(uint16_t sender_ohm, int16_t speed_kph, int16_t accel_cms2, bool refuel_lid_open);
bool bcm_fuel_low(void);
void bcm_fuel_reset(void);
bool bcm_driver_seat_store(uint8_t slot, bcm_power_t power);
bool bcm_driver_seat_recall(uint8_t slot, int16_t speed_kph);
bool bcm_driver_seat_move(bool manual_switch, bool obstacle);
void bcm_driver_seat_set(int16_t fore_aft, int16_t height, int16_t recline);
int16_t bcm_driver_seat_fore_aft(void);
void bcm_driver_seat_reset(void);
bool bcm_passenger_seat_store(uint8_t slot, bcm_power_t power);
bool bcm_passenger_seat_recall(uint8_t slot, int16_t speed_kph);
bool bcm_passenger_seat_move(bool manual_switch, bool obstacle);
void bcm_passenger_seat_set(int16_t fore_aft, int16_t height, int16_t recline);
int16_t bcm_passenger_seat_fore_aft(void);
void bcm_passenger_seat_reset(void);
bool bcm_high_beam(bool lever_high, bool flash, bool assist_on, bool oncoming, bool preceding, int32_t lux, int16_t speed_kph, uint16_t dt_ms);
void bcm_high_beam_reset(void);
int8_t bcm_gateway_route(uint32_t id, uint8_t from_bus, bool diagnostic_session, uint16_t now_ms_mod);
void bcm_gateway_reset(void);
bcm_nm_t bcm_nm_step(bool local_wake, bool bus_activity, bool keep_awake, uint16_t dt_ms);
void bcm_nm_reset(void);
void bcm_supervision_checkpoint(uint8_t task);
bool bcm_supervision_cycle(uint8_t expected_mask);
void bcm_supervision_reset(void);
bool bcm_washer_low(bool level_switch_low, int16_t accel_cms2, uint16_t dt_ms);
bool bcm_rear_seat_reminder(bool rear_door_opened, bcm_power_t power, bool driver_door_open);
void bcm_misc_reset(void);
bcm_slide_t bcm_slide_door_left(bool request, bool child_lock_inner, bool inner_request, bool locked, int16_t speed_kph, bool fuel_lid_open);
int16_t bcm_slide_door_left_position(void);
uint8_t bcm_slide_door_left_obstacles(void);
void bcm_slide_door_left_reset(void);
bcm_slide_t bcm_slide_door_right(bool request, bool child_lock_inner, bool inner_request, bool locked, int16_t speed_kph, bool fuel_lid_open);
int16_t bcm_slide_door_right_position(void);
uint8_t bcm_slide_door_right_obstacles(void);
void bcm_slide_door_right_reset(void);
bool bcm_wheel_heater(bool button, int16_t wheel_temp, bcm_power_t power, uint8_t shed_level, uint16_t dt_ms);
int16_t bcm_headlamp_level(int16_t pitch, int16_t speed_kph, bool static_mode);
uint8_t bcm_cornering(int16_t steering_deg, bcm_turn_t turn, bool low_beam, int16_t speed_kph, bool reverse);
void bcm_lamps_reset(void);

#endif /* BCM_H */
