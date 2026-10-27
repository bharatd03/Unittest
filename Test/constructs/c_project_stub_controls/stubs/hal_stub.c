#include "hal.h"
#include "hal_stub.h"

static uint32_t s_echo_us;
static bool s_no_echo;

void sim_set_echo_us(uint32_t us) { s_echo_us = us; }
void sim_set_no_echo(bool on) { s_no_echo = on; }
void sim_reset(void) { s_echo_us = 0u; s_no_echo = false; }

void hal_trigger(void) { }
uint32_t hal_echo_width_us(void) { return s_no_echo ? 0u : s_echo_us; }
