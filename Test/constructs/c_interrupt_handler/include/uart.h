/* uart.h */
#ifndef UART_H
#define UART_H
void uart_rx_isr(void);
/* @return 1 when a byte was received since the last call */
int uart_poll(void);
#endif
