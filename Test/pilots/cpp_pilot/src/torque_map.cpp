// Pilot SUT (C++, ASIL D powertrain): std::array tables, a search loop, interpolation, a const method and
// a free function with a four-condition decision. Every line and condition is reachable.
#include "torque_map.h"

TorqueMap::TorqueMap() : rpm_{{0, 1000, 2000, 4000, 6000}}, torque_{{80, 180, 260, 320, 240}}
{
}

int TorqueMap::max_torque(int rpm) const
{
    if (rpm < rpm_.front() || rpm > rpm_.back()) {
        return 0;
    }
    std::size_t i = 1;
    while (rpm > rpm_[i]) {          // ends at the last point at the latest: rpm <= rpm_.back()
        ++i;
    }
    return torque_[i - 1] + (torque_[i] - torque_[i - 1]) * (rpm - rpm_[i - 1]) / (rpm_[i] - rpm_[i - 1]);
}

int TorqueMap::limit(int rpm, int request, bool limp) const
{
    int top = max_torque(rpm);
    if (limp) {
        top /= 2;
    }
    if (request < 0) {
        return 0;
    }
    return request > top ? top : request;
}

bool valid_request(int rpm, int request, bool gear_engaged, bool clutch_open)
{
    return rpm >= 500 && rpm <= 6000 && request >= 0 && request <= 400 && (gear_engaged || clutch_open);
}
