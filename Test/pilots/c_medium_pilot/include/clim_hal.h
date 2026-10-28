/* clim_hal.h - the hardware abstraction and services the climate control uses (implemented per ECU). */
#ifndef CLIM_HAL_H
#define CLIM_HAL_H

#include <stdbool.h>
#include <stdint.h>

#define CLIM_ADC_CABIN_BASE 0u     /* channels 0..3: cabin sensors of the four zones */
#define CLIM_ADC_OUTSIDE 4u
#define CLIM_ADC_EVAP 5u
#define CLIM_ADC_PRESSURE 6u
#define CLIM_ADC_SOLAR 7u

#define CLIM_PWM_BLOWER 0u
#define CLIM_PWM_MIX_BASE 1u       /* channels 1..4: mix doors */

#define CLIM_PIN_COMPRESSOR 1u
#define CLIM_PIN_RECIRC 2u
#define CLIM_PIN_REAR_DEFROST 3u

#define CLIM_CAN_ID_STATUS 0x3A1u

/* memory-mapped status and diagnosis registers of the climate ASIC */
#define CLIM_STATUS_REG (*(volatile uint32_t *)0x40021000u)
#define CLIM_DIAG_REG (*(volatile uint32_t *)0x40021004u)
#define CLIM_STATUS_READY 0x1u

uint16_t hal_adc_read(uint8_t channel);
void hal_pwm_set(uint8_t channel, uint8_t duty_pct);
void hal_gpio_write(uint8_t pin, bool level);
bool can_send(uint16_t id, const uint8_t *data, uint8_t len);
bool nvm_read(uint8_t block, uint8_t *buffer, uint16_t size);
bool nvm_write(uint8_t block, const uint8_t *buffer, uint16_t size);

#endif /* CLIM_HAL_H */
