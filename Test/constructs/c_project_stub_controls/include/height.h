#ifndef HEIGHT_H
#define HEIGHT_H
#include <stdint.h>
typedef enum { HEIGHT_OK = 0, HEIGHT_ERR_NO_ECHO = 1, HEIGHT_ERR_RANGE = 2 } height_status_t;
uint32_t height_ping_us(void);
height_status_t height_distance_mm(int32_t *mm);
#endif
