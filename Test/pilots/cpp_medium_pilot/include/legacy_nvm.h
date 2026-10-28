/* legacy_nvm.h - the non-volatile memory driver, a C library (global namespace, C linkage). */
#ifndef LEGACY_NVM_H
#define LEGACY_NVM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int nvm_load(uint16_t id, void *data, uint16_t size);
int nvm_store(uint16_t id, const void *data, uint16_t size);

#ifdef __cplusplus
}
#endif

#endif
