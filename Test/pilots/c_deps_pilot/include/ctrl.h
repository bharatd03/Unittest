#ifndef CTRL_H
#define CTRL_H
typedef struct { int lo; int hi; } range_t;
typedef enum { MODE_OFF, MODE_ON = 5, MODE_AUTO } mode_t2;
/** @return 1 when v is inside r, 0 otherwise, -1 for a NULL range */
int in_range(const range_t *r, int v);
int control(int x, unsigned flags, mode_t2 m);
#endif
