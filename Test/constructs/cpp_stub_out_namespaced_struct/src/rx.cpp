// rx.cpp - a dependency in a namespace fills a structure of that namespace through a reference
#include "rx.hpp"
#include "drv.hpp"
int receive() {
    drv::can::Frame frame{};
    if (!drv::can::read(frame)) {
        return -1;
    }
    if (frame.id != 0x2B4u) {
        return -2;
    }
    if (frame.len != 8u) {
        return -3;
    }
    return frame.data[0];
}
