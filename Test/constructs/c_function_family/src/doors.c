#include "doors.h"

static uint8_t fl_open, fl_locked, fl_window, fl_alive;
static uint32_t fl_count, fl_errors;
static uint8_t fr_open, fr_locked, fr_window, fr_alive;
static uint32_t fr_count, fr_errors;
static uint8_t rl_open, rl_locked, rl_window, rl_alive;
static uint32_t rl_count, rl_errors;
static uint8_t rr_open, rr_locked, rr_window, rr_alive;
static uint32_t rr_count, rr_errors;

int door_fl_receive(const uint8_t *data, uint8_t len)
{
    if (data == 0 || len != 4u) {
        return -1;
    }
    if (data[0] != DOOR_FL_ID) {
        return -2;
    }
    fl_open = (uint8_t)((data[1] & 0x01u) != 0u);
    fl_locked = (uint8_t)((data[1] & 0x02u) != 0u);
    fl_window = data[2];
    fl_alive = data[3];
    fl_count++;
    if (fl_open != 0u && fl_locked != 0u) {
        fl_errors++;
        return -3;
    }
    return 0;
}

int door_fr_receive(const uint8_t *data, uint8_t len)
{
    if (data == 0 || len != 4u) {
        return -1;
    }
    if (data[0] != DOOR_FR_ID) {
        return -2;
    }
    fr_open = (uint8_t)((data[1] & 0x01u) != 0u);
    fr_locked = (uint8_t)((data[1] & 0x02u) != 0u);
    fr_window = data[2];
    fr_alive = data[3];
    fr_count++;
    if (fr_open != 0u && fr_locked != 0u) {
        fr_errors++;
        return -3;
    }
    return 0;
}

int door_rl_receive(const uint8_t *data, uint8_t len)
{
    if (data == 0 || len != 4u) {
        return -1;
    }
    if (data[0] != DOOR_RL_ID) {
        return -2;
    }
    rl_open = (uint8_t)((data[1] & 0x01u) != 0u);
    rl_locked = (uint8_t)((data[1] & 0x02u) != 0u);
    rl_window = data[2];
    rl_alive = data[3];
    rl_count++;
    if (rl_open != 0u && rl_locked != 0u) {
        rl_errors++;
        return -3;
    }
    return 0;
}

int door_rr_receive(const uint8_t *data, uint8_t len)
{
    if (data == 0 || len != 4u) {
        return -1;
    }
    if (data[0] != DOOR_RR_ID) {
        return -2;
    }
    rr_open = (uint8_t)((data[1] & 0x01u) != 0u);
    rr_locked = (uint8_t)((data[1] & 0x02u) != 0u);
    rr_window = data[2];
    rr_alive = data[3];
    rr_count++;
    if (rr_open != 0u && rr_locked != 0u) {
        rr_errors++;
        return -3;
    }
    return 0;
}
