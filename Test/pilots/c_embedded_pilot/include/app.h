/* app.h - the application step called from the main loop. */
#ifndef APP_H
#define APP_H
/** One pass of the main loop. @return the new mode: 0 idle, 1 run, 2 fault. */
int app_step(int mode, int button, int fault);
#endif
