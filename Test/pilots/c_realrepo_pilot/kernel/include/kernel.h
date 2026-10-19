/* kernel.h - the configuration every kernel header needs first (like FreeRTOS.h). */
#ifndef KERNEL_H
#define KERNEL_H
#include <stdint.h>
#include <stddef.h>
typedef uint32_t kTick_t;
typedef int32_t kBase_t;
#define KLIST_MAX 8U
#define KQUEUE_TIMEOUT 100U
#endif
