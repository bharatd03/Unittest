// encoder.hpp - interface of the position encoder; the board implementation is injected.
#pragma once
#include <cstdint>

class IEncoder {
public:
    virtual ~IEncoder() = default;
    virtual std::int32_t position() const = 0;
    virtual bool fault() const = 0;
};
