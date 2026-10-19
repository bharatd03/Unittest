# Embedded C++ pilot requirements

- CTRL-1: MotorCtrl::step throws std::invalid_argument for a target outside -100000..100000.
- CTRL-2: MotorCtrl::step returns 0 while the encoder reports a fault.
- CTRL-3: MotorCtrl::step clamps the command to -1000..1000.
- CTRL-4: MotorCtrl::start returns false for a period of 0 or above 65535.
- CTRL-5: MotorCtrl::sat_add returns 0xFFFFFFFF when the 32-bit sum overflows.
- APP-1: app::step returns 2 whenever fault is set.
