// System clock: not part of the SUT, so tests need a stub.
#ifndef CLOCK_H
#define CLOCK_H
/** Milliseconds since start-up. */
long long now_ms();
#endif
