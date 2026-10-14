# Rate limiter, steering monitor and torque map requirements

- RLM-1: allow returns true while tokens are left in the current window and false when the limit is reached; a new window starts after window_ms milliseconds.
- RLM-2: valid_burst returns true when the burst lies in 1..capacity and maintenance mode is off.

## Steering monitor (ASIL D)

- SMN-1: SteeringMonitor uses 1 for a limit that is not above 0.
- SMN-2: check returns Fault and latches it when the sensor is not valid; a latched Fault is kept until reset.
- SMN-3: an angle outside -max_angle..max_angle or a rate outside -max_rate..max_rate is a violation: check returns Warning.
- SMN-4: a violation within 100 ms of the previous one continues the series; the third violation of a series returns Fault.
- SMN-5: reset returns the monitor to Ok with no violations.

## Torque map (ASIL D)

- TQM-1: the map holds 80, 180, 260, 320 and 240 Nm at 0, 1000, 2000, 4000 and 6000 rpm.
- TQM-2: max_torque interpolates linearly between the points and returns 0 outside 0..6000 rpm.
- TQM-3: limit returns 0 for a negative request, else the request limited to the maximum torque, halved in limp mode.
- TQM-4: valid_request is true for 500..6000 rpm and 0..400 Nm, with the gear engaged or the clutch open.
