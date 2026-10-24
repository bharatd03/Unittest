/* uart.c */
#include "uart.h"
int uart_send(uint8_t byte) {
    while (0u == (UART_SR & UART_TXE)) {
    }
    UART_DR = byte;
    return byte;
}
int uart_flush(void) {
    while (READ_BIT(UART_SR, UART_BUSY)) {
    }
    return 0;
}
