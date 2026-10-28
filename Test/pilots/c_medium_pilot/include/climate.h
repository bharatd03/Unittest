/* climate.h - automatic climate control of a passenger car: four zones, compressor, blower, recirculation,
   rear defrost, CAN interface and stored parameters. */
#ifndef CLIMATE_H
#define CLIMATE_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    CLIM_MODE_OFF = 0,
    CLIM_MODE_AUTO,
    CLIM_MODE_MANUAL,
    CLIM_MODE_DEFROST,
    CLIM_MODE_ECO
} clim_mode_t;

typedef enum {
    CLIM_CMD_NONE = 0,
    CLIM_CMD_ON,
    CLIM_CMD_OFF,
    CLIM_CMD_DEFROST,
    CLIM_CMD_ECO,
    CLIM_CMD_MANUAL
} clim_cmd_t;

typedef enum {
    CLIM_COMP_OFF = 0,
    CLIM_COMP_STARTING,
    CLIM_COMP_RUNNING,
    CLIM_COMP_PROTECT,
    CLIM_COMP_LOCKED
} clim_comp_state_t;

typedef enum {
    CLIM_ZONE_DRIVER = 0,
    CLIM_ZONE_PASSENGER,
    CLIM_ZONE_REAR_LEFT,
    CLIM_ZONE_REAR_RIGHT,
    CLIM_ZONE_COUNT
} clim_zone_t;

typedef enum {
    CLIM_FAULT_NONE = 0,
    CLIM_FAULT_SENSOR,
    CLIM_FAULT_BLOWER,
    CLIM_FAULT_COMPRESSOR,
    CLIM_FAULT_PRESSURE,
    CLIM_FAULT_COUNT
} clim_fault_t;

typedef enum {
    CLIM_CAN_OK = 0,
    CLIM_CAN_ERR_LEN,
    CLIM_CAN_ERR_CRC,
    CLIM_CAN_ERR_ALIVE,
    CLIM_CAN_ERR_RANGE
} clim_can_result_t;

typedef struct {
    int16_t setpoint_dc;      /* deci-degrees C */
    uint8_t fan_level;        /* 0 = automatic, 1..7 manual */
    bool recirc;
    bool ac_request;
} clim_request_t;

typedef struct {
    int16_t cabin_dc[CLIM_ZONE_COUNT];
    int16_t outside_dc;
    int16_t evap_dc;
    uint16_t pressure_kpa;
    uint8_t solar_pct;
    uint16_t pm25;
    uint16_t supply_mv;
} clim_inputs_t;

typedef struct {
    uint8_t blower_pct;
    uint8_t mix_pct[CLIM_ZONE_COUNT];
    bool compressor;
    bool recirc;
    bool rear_defrost;
} clim_outputs_t;

typedef void (*clim_fault_cb_t)(clim_fault_t fault, bool active);

/* signal conditioning */
int16_t clim_interp(const int16_t *xs, const int16_t *ys, uint8_t n, int16_t x);
uint8_t clim_crc8(const uint8_t *data, uint8_t len);
int16_t clim_filter_temp(int16_t previous_dc, int16_t sample_dc, uint8_t alpha_pct);
bool clim_raw_valid(uint16_t raw);
int16_t clim_raw_to_dc(uint16_t raw);
bool clim_read_inputs(clim_inputs_t *in);

/* faults */
void clim_fault_register_cb(clim_fault_cb_t cb);
void clim_fault_report(clim_fault_t fault, bool failed);
bool clim_fault_active(clim_fault_t fault);
uint8_t clim_fault_count_confirmed(void);
void clim_diag_flags_update(void);
bool clim_hw_ready(void);

/* modes, compressor, blower, mix */
clim_mode_t clim_mode_step(clim_cmd_t cmd, bool engine_running, uint16_t dt_ms);
clim_mode_t clim_mode(void);
clim_comp_state_t clim_compressor_step(bool request, int16_t outside_dc, uint16_t pressure_kpa, int16_t evap_dc,
                                       uint16_t dt_ms);
clim_comp_state_t clim_comp_state(void);
uint8_t clim_blower_pct(clim_mode_t mode, uint8_t fan_level, int16_t error_dc, bool engine_running);
uint8_t clim_mix_pct(clim_zone_t zone, int16_t setpoint_dc, int16_t cabin_dc, uint8_t solar_pct);

/* zones */
bool clim_zone_set(clim_zone_t zone, int16_t setpoint_dc);
int16_t clim_zone_setpoint(clim_zone_t zone);
void clim_sync(bool on);
void clim_rear_enable(bool on);

/* recirculation and rear defrost */
bool clim_recirc_step(bool request, bool defrost, uint16_t pm25, uint16_t dt_ms);
bool clim_rear_defrost(bool button, uint16_t supply_mv, uint16_t dt_ms);

/* CAN and parameters */
clim_can_result_t clim_can_parse(const uint8_t *frame, uint8_t len, clim_request_t *req);
bool clim_can_encode(const clim_outputs_t *out, uint8_t *frame, uint8_t len);
bool clim_can_send_status(const clim_outputs_t *out);
bool clim_params_load(void);
bool clim_params_save(void);
int16_t clim_default_setpoint(void);

/* the cycle */
void clim_init(void);
void clim_step(const clim_inputs_t *in, const clim_request_t *req, clim_outputs_t *out, uint16_t dt_ms);

#endif /* CLIMATE_H */
