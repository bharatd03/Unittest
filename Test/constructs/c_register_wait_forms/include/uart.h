/* uart.h - a peripheral outside the Cortex-M address windows */
#ifndef UART_H
#define UART_H
#include <stdint.h>
#define PERIPH_BASE (0xF0001000UL)
#define UART_SR     (*(volatile uint32_t *)(PERIPH_BASE + 0x04u))
#define UART_DR     (*(volatile uint32_t *)(PERIPH_BASE + 0x08u))
#define UART_TXE    (1u << 7)
#define UART_BUSY   (1u << 3)
#define READ_BIT(REG, BIT) ((REG) & (BIT))
/* @return the byte sent */
int uart_send(uint8_t byte);
/* @return 0 when the line went idle */
int uart_flush(void);
#endif
