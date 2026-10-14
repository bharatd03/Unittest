# Controller requirements

- CTL-1: in_range returns 1 when v lies inside the range r (bounds included), 0 when it lies outside, and -1 when r is NULL.
- CTL-2: control returns 0 while the mode is MODE_OFF.
- CTL-3: control returns 7 when the override flag (bit 1 of flags, value 2) is set and 5 < x < 30, unless the hardware path applies (status bit 0 set and the hardware reading above MINI_LIMIT).
- CTL-4: otherwise control returns x limited to -MINI_LIMIT..MINI_LIMIT.

# Brake controller requirements (ASIL D)

- BRK-1: brake_step returns BRK_FAULT when the input is NULL or the status reports a fault (BRK_STATUS_FAULT), and counts the fault.
- BRK-2: from BRK_IDLE a pedal above 10 % arms the brake; from BRK_ARMED the brake becomes active when the pedal is above 10 % and the vehicle moves faster than 3 km/h or hill-hold is requested; an active brake stays active while the pedal is above 5 %; BRK_FAULT is kept until reset.
- BRK-3: brake_pressure_request returns -1 for a pedal value outside 0..100.
- BRK-4: brake_pressure_request reads both pressure sensors; when they differ by more than 5 bar it counts a fault and returns -1, otherwise it returns the pedal value plus half the sensor mean, limited to BRK_MAX_BAR.
- BRK-5: brake_fault_count returns the number of faults counted since start-up.

# Steering assist requirements (ASIL D)

- STR-1: steer_gain interpolates the assist gain linearly between 100 % at 0 km/h, 80 % at 30 km/h, 50 % at 60 km/h and 20 % at 120 km/h, and keeps 20 % above 120 km/h.
- STR-2: steer_assist_torque adds the lane nudge STEER_LANE_NUDGE when lane assist is on, the speed is at least 60 km/h and the driver torque lies strictly between -STEER_HANDS_ON and STEER_HANDS_ON.
- STR-3: the assist torque is limited to -STEER_MAX..STEER_MAX.
- STR-4: the assist torque changes by at most STEER_RATE per call.
- STR-5: steer_reset restarts the rate limiter from 0.
