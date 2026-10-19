/* gpio_driver.h - GPIO and UART access through the register map. */
#ifndef GPIO_DRIVER_H
#define GPIO_DRIVER_H
#include <stdint.h>
/** Enables the GPIOA clock and makes pin 0 an output. */
void gpio_init(void);
/** @return 0 when the pin was set or cleared, -1 for a pin above 15. */
int gpio_set(unsigned pin, int on);
/** @return 0 when READY_PIN went high within max_loops polls, -1 otherwise. */
int gpio_wait_ready(uint32_t max_loops);
/** Waits until the transmitter is empty and sends one byte. */
void uart_put(uint8_t c);
#endif
