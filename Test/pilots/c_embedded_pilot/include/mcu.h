/* mcu.h - register map of the microcontroller, in the style of a vendor (CMSIS) device header. */
#ifndef MCU_H
#define MCU_H
#include <stdint.h>

typedef struct {
    volatile uint32_t MODER;
    volatile uint32_t IDR;
    volatile uint32_t ODR;
    volatile uint32_t BSRR;
} GPIO_TypeDef;

#define PERIPH_BASE   0x40000000UL
#define GPIOA_BASE    (PERIPH_BASE + 0x20000UL)
#define GPIOA         ((GPIO_TypeDef *)GPIOA_BASE)
#define RCC_AHB1ENR   (*(volatile uint32_t *)0x40023830UL)
#define UART1_SR      (*(volatile uint32_t *)0x40011000UL)
#define UART1_DR      (*(volatile uint32_t *)0x40011004UL)

#define GPIOA_EN      (1UL << 0)
#define READY_PIN     (1UL << 5)
#define UART_TXE      (1UL << 7)
#endif
