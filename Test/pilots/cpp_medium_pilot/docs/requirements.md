# Chassis control requirements

- SPD-1: the vehicle speed is the moving average of four values of the mean of the two middle wheel speeds
- SPD-2: a wheel speed above 300.0 km/h, or a spread between the fastest and the slowest wheel above 20.0 km/h, is implausible; after 3 implausible updates in a row the last estimate is held
- BRK-1: with ABS active or without a brake request, the whole request is braked by friction; otherwise regeneration takes up to its maximum, and only half of it while the diagnosis level is 2 or more
- BRK-2: a regeneration maximum of -1 or below is invalid: diagnosis code 0x101 is reported, a fault is counted and the whole request is braked by friction
- BRK-3: a regeneration that drops by more than 500 from one cycle to the next for 200000 ms reports diagnosis code 0x102
- STR-1: the steering request is limited to the rate given at construction, per second; a rate of 0 or below is refused with an exception
- TRC-1: the slip of the faster rear wheel above the vehicle speed is averaged over eight updates; below 3.0 km/h a slower wheel counts as slip too
- TRC-2: an average slip above 4.0 km/h (8.0 km/h in Sport) with a positive torque request cuts the torque by the slip in percent, at most 100 %; every tenth intervention reports diagnosis code 0x201
- LVL-1: in Sport and Eco the ride height is lowered by 20 mm above 120 km/h and raised again below 100 km/h; Comfort uses the stored comfort offset; Service holds the height
- LVL-2: a height within 5 mm of the target settles after 60000 ms; once settled, deviations below 10 mm are not corrected
- CAN-1: the status frame 0x2B0 carries the speed (little endian), the mode, the plausibility, an alive counter and a CRC-8 (polynomial 0x2F, start 0) of bytes 0 to 6 in byte 7
- CAN-2: a mode request is accepted only from frame 0x2B4 with 8 bytes, a valid CRC-8, a new alive counter and a mode of Comfort, Sport or Eco
- PAR-1: stored parameters are used only with version 3, a valid CRC-8 and a comfort offset of -30 to 30 mm and a sport gain of 50 to 200 %; otherwise the defaults 10 mm and 120 % are used
- MOD-1: a short press selects the next driving mode Comfort, Sport, Eco and again Comfort; a long press selects Service only at standstill
- ACT-1: the axle duty is the demand times the sport gain in percent, 0 to 1000; below 9000 mV supply it is 0 and below 11000 mV half; the rear stage is driven inverted
- PMP-1: the pump runs on request while the pressure is below 1800 and stops without request or above 2200; a low fluid level stops it and counts a fault
- PMP-2: the pump stops after 300000 ms of accumulated running time; the time decreases while it is off
- DIA-1: codes 0x301 to 0x304 name the front left, front right, rear left and rear right actuator
