# Requirements of the C++ real-repository pilot

- RRC-1: dispatch shall report an error (onError) and return -1 when no event is given.
- RRC-2: dispatch shall post events with a signal above 10 and return 1 when the post succeeds, 0 otherwise.
- RRC-3: dispatch shall return 2 for signals up to 10.
