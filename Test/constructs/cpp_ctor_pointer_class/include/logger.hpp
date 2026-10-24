// logger.hpp
#pragma once
class Buffer {
public:
    int used = 0;
    int capacity = 4;
};
class Logger {
public:
    explicit Logger(Buffer* buffer) : buffer_(buffer) {}
    /// @return 0 when stored, -1 when the buffer is full or missing.
    int log(int value);
private:
    Buffer* buffer_;
};
