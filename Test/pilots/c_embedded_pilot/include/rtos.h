/* rtos.h - the part of the RTOS interface the application uses. */
#ifndef RTOS_H
#define RTOS_H
#include <stdint.h>
typedef void *QueueHandle_t;
typedef uint32_t TickType_t;
#define portMAX_DELAY 0xFFFFFFFFUL
#define pdTRUE 1
int xQueueReceive(QueueHandle_t queue, void *item, TickType_t wait);
TickType_t xTaskGetTickCount(void);
void vTaskDelay(TickType_t ticks);
#endif
