/* bcm_hal.h - the hardware abstraction the body control module uses (implemented per ECU). */
#ifndef BCM_HAL_H
#define BCM_HAL_H

#include <stdbool.h>
#include <stdint.h>

#define BCM_PIN_LOW_BEAM 1u
#define BCM_PIN_TURN_LEFT 2u
#define BCM_PIN_TURN_RIGHT 3u
#define BCM_PIN_WIPER_PARK_F 4u
#define BCM_PIN_WIPER_PARK_R 5u
#define BCM_PIN_LOCK_ALL 6u
#define BCM_PIN_UNLOCK_ALL 7u
#define BCM_PIN_WIN_FL_UP 8u
#define BCM_PIN_WIN_FL_DN 9u
#define BCM_PIN_WIN_FR_UP 10u
#define BCM_PIN_WIN_FR_DN 11u
#define BCM_PIN_WIN_RL_UP 12u
#define BCM_PIN_WIN_RL_DN 13u
#define BCM_PIN_WIN_RR_UP 14u
#define BCM_PIN_WIN_RR_DN 15u
#define BCM_PIN_HORN 16u
#define BCM_PIN_MIRROR_FOLD 17u
#define BCM_PIN_MIRROR_HEAT 18u
#define BCM_PIN_SLIDE_L_OPEN 19u
#define BCM_PIN_SLIDE_L_CLOSE 20u
#define BCM_PIN_SLIDE_R_OPEN 21u
#define BCM_PIN_SLIDE_R_CLOSE 22u

#define BCM_ADC_WIN_FL 0u
#define BCM_ADC_WIN_FR 1u
#define BCM_ADC_WIN_RL 2u
#define BCM_ADC_WIN_RR 3u
#define BCM_ADC_SEAT_DRV 4u
#define BCM_ADC_SEAT_PAS 5u
#define BCM_ADC_SLIDE_L 6u
#define BCM_ADC_SLIDE_R 7u
#define BCM_ADC_SEAT_RL 8u
#define BCM_ADC_SEAT_RR 9u

#define BCM_PWM_WIPER_F 0u
#define BCM_PWM_WIPER_R 1u
#define BCM_PWM_SEAT_DRV 2u
#define BCM_PWM_SEAT_PAS 3u
#define BCM_PWM_INTERIOR 4u
#define BCM_PWM_SEAT_RL 5u
#define BCM_PWM_SEAT_RR 6u

void hal_gpio_write(uint8_t pin, bool level);
bool hal_gpio_read(uint8_t pin);
uint16_t hal_adc_read(uint8_t channel);
void hal_pwm_set(uint8_t channel, uint8_t duty);
bool nvm_read(uint8_t block, uint8_t *buffer, uint16_t size);
bool nvm_write(uint8_t block, const uint8_t *buffer, uint16_t size);

#endif /* BCM_HAL_H */
