/* crc.h */
#ifndef CRC_H
#define CRC_H
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
unsigned char crc8(const unsigned char *data, size_t len);
#ifdef __cplusplus
}
#endif
#endif
