// Pilot SUT (C++, ASIL D powertrain): engine torque map with interpolation and request checks.
#ifndef TORQUE_MAP_H
#define TORQUE_MAP_H

#include <array>

class TorqueMap {
public:
    /** TQM-1: maximum torque (Nm) at 0, 1000, 2000, 4000 and 6000 rpm. */
    TorqueMap();
    /** TQM-2: the maximum torque for an engine speed, interpolated linearly; 0 below 0 rpm and above 6000 rpm.
     *  @return torque in Nm */
    int max_torque(int rpm) const;
    /** TQM-3: the requested torque limited to 0..max_torque(rpm); in limp mode to half of it. */
    int limit(int rpm, int request, bool limp) const;

private:
    std::array<int, 5> rpm_;
    std::array<int, 5> torque_;
};

/** TQM-4: a request is valid when the speed lies in 500..6000 rpm and the request in 0..400 Nm,
 *  and the gear is engaged or the clutch is open. */
bool valid_request(int rpm, int request, bool gear_engaged, bool clutch_open);

#endif
