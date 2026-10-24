// heater.hpp
#pragma once
#include "sensor.hpp"
class Heater {
public:
    explicit Heater(ISensor& sensor) : sensor_(sensor) {}
    /// @return 1 when the reading is below the set point, else 0.
    int demand(int set_point) const;
private:
    ISensor& sensor_;
};
