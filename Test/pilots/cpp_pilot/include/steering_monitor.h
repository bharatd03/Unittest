// Pilot SUT (C++, ASIL D steering): plausibility monitor for the steering angle sensor.
#ifndef STEERING_MONITOR_H
#define STEERING_MONITOR_H

enum class MonitorState { Ok, Warning, Fault };

class SteeringMonitor {
public:
    /** SMN-1: limits for the angle (degrees) and the angle rate (degrees per second), both > 0. */
    SteeringMonitor(int max_angle_deg, int max_rate_dps);
    /** SMN-2 .. SMN-4: checks one sample.
     *  @return Fault for an invalid sensor or the third violation within 100 ms, Warning for a violation,
     *          Ok otherwise; Fault is kept until reset() */
    MonitorState check(int angle_deg, int rate_dps, bool sensor_valid);
    /** @return the violations counted in the current series */
    int violations() const;
    /** SMN-5: back to Ok, no violations. */
    void reset();

private:
    int max_angle_;
    int max_rate_;
    int violations_;
    long long last_violation_ms_;
    MonitorState state_;
};

#endif
