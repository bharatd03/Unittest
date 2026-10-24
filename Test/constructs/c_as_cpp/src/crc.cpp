/* crc.cpp - C code built as C++ */
#include "crc.h"
extern "C" unsigned char crc8(const unsigned char *data, size_t len) {
    unsigned char crc = 0;
    size_t i;
    if (data == NULL) {
        return 0;
    }
    for (i = 0; i < len; ++i) {
        crc = (unsigned char)(crc ^ data[i]);
    }
    return crc;
}
