# Echo sensor requirements

- ECHO-1: echo_set_temperature limits the temperature to -40.0 .. 85.0 degC.
- ECHO-2: echo_distance_mm returns -1 and status ECHO_NO_START when the echo does not start within 5 ms.
- ECHO-3: echo_distance_mm returns -1 and status ECHO_TIMEOUT when the echo lasts longer than 30 ms.
