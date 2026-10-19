/* crc_regs.h - a peripheral and its handle (STM32 HAL style). */
#ifndef CRC_REGS_H
#define CRC_REGS_H
#include <stdint.h>
typedef struct {
    volatile uint32_t DR;
    volatile uint32_t CR;
} CRC_Regs;
typedef struct {
    CRC_Regs *Instance;
    uint32_t State;
} crc_handle_t;
uint32_t crc_calc(crc_handle_t *hcrc, const uint32_t buf[], uint32_t len);
#endif
