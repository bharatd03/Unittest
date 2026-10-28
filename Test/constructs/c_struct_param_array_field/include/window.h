/* window.h */
#ifndef WINDOW_H
#define WINDOW_H
typedef struct {
    int count;
    int samples[4];
} window_t;
/* @return the largest of the first count samples (0 when none is positive), -1 for NULL */
int window_peak(const window_t *w);
#endif
