#ifndef DOORS_H
#define DOORS_H
#include <stdint.h>
#define DOOR_FL_ID 0xA1u
#define DOOR_FR_ID 0xA2u
#define DOOR_RL_ID 0xA3u
#define DOOR_RR_ID 0xA4u
int door_fl_receive(const uint8_t *data, uint8_t len);
int door_fr_receive(const uint8_t *data, uint8_t len);
int door_rl_receive(const uint8_t *data, uint8_t len);
int door_rr_receive(const uint8_t *data, uint8_t len);
#endif
