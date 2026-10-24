/* door.h */
#ifndef DOOR_H
#define DOOR_H
/* events: 0 open request, 1 close request, 2 lock request.
   @return the new state: 0 closed, 1 open, 2 locked */
int door_step(int event);
#endif
