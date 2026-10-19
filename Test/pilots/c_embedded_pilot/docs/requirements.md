# Embedded C pilot requirements

- EMB-1: gpio_set returns -1 for a pin above 15 and 0 otherwise.
- EMB-2: gpio_wait_ready returns -1 when READY_PIN does not go high within max_loops polls.
- EMB-3: sched_debounce changes its output only after the same new input in 3 calls in a row.
- EMB-4: sched_sat_add returns 0xFFFFFFFF when the 32-bit sum overflows.
- EMB-5: app_step returns 2 (fault) whenever fault is set.
