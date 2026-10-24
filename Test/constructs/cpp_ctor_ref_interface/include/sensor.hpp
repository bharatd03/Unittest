// sensor.hpp - the injected interface
#pragma once
class ISensor {
public:
    virtual ~ISensor() = default;
    virtual int read() const = 0;
};
