/* uart.c */
#include "uart.h"
static volatile int g_rx = 0;
void uart_rx_isr(void) {
    g_rx = 1;
}
int uart_poll(void) {
    if (g_rx) {
        g_rx = 0;
        return 1;
    }
    return 0;
}
