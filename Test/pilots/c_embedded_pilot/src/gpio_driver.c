/* gpio_driver.c - direct register access, as embedded drivers do it. */
#include "gpio_driver.h"
#include "mcu.h"

void gpio_init(void)
{
    RCC_AHB1ENR |= GPIOA_EN;
    GPIOA->MODER = (GPIOA->MODER & ~0x3UL) | 0x1UL;
}

int gpio_set(unsigned pin, int on)
{
    if (pin > 15u) {
        return -1;
    }
    if (on) {
        GPIOA->BSRR = 1UL << pin;
    } else {
        GPIOA->BSRR = 1UL << (pin + 16u);
    }
    return 0;
}

int gpio_wait_ready(uint32_t max_loops)
{
    uint32_t n = 0u;
    while ((GPIOA->IDR & READY_PIN) == 0u) {
        if (++n > max_loops) {
            return -1;
        }
    }
    return 0;
}

void uart_put(uint8_t c)
{
    while ((UART1_SR & UART_TXE) == 0u) {
        /* the hardware sets TXE when the byte has left */
    }
    UART1_DR = c;
}
