/* events.h */
#ifndef EVENTS_H
#define EVENTS_H
typedef void (*event_cb)(int code);
void events_register(event_cb cb);
/* @return 1 when a callback was called, 0 without one */
int events_raise(int code);
#endif
