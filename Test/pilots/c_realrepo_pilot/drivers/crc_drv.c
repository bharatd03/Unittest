/* crc_drv.c - a driver that reaches its registers through the handle's Instance pointer. */
#include "crc_regs.h"
#include <stddef.h>

uint32_t crc_calc(crc_handle_t *hcrc, const uint32_t buf[], uint32_t len)
{
    uint32_t i;
    if (hcrc == NULL) {
        return 0U;
    }
    hcrc->State = 1U;
    hcrc->Instance->CR = 1U;
    for (i = 0U; i < len; i++) {
        hcrc->Instance->DR = buf[i];
    }
    hcrc->State = 0U;
    return hcrc->Instance->DR;
}
