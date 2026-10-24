// logger.cpp
#include "logger.hpp"
int Logger::log(int value) {
    if (buffer_ == nullptr || buffer_->used >= buffer_->capacity) {
        return -1;
    }
    buffer_->used += (value != 0) ? 1 : 1;
    return 0;
}
