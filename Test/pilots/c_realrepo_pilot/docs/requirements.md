# Requirements of the real-repository pilot

- RR-1: klist_insert_end shall refuse an item when the list holds KLIST_MAX items and return -1.
- RR-2: kqueue_wait shall yield when more than KQUEUE_TIMEOUT ticks have passed since start.
- RR-3: ring_free shall return 0 for a missing or empty buffer.
- RR-4: crc_calc shall return 0 when no handle is given.
