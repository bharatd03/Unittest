/* status.h */
#ifndef STATUS_H
#define STATUS_H
typedef union {
    unsigned int raw;
    struct {
        unsigned int ready : 1;
        unsigned int error : 1;
        unsigned int code : 6;
    } bits;
} status_t;
/* @return 2 on error, 1 when ready, 0 otherwise */
int status_eval(status_t s);
#endif
