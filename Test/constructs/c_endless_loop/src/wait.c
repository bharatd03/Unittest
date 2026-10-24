/* wait.c */
#include "wait.h"
#include "flag.h"
int wait_ready(int max) {
    int polls = 0;
    while (!flag_ready()) {
        ++polls;
        if (polls >= max) {
            return -1;
        }
    }
    return polls;
}
