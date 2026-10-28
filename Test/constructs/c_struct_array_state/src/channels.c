/* channels.c */
#include "channels.h"
typedef struct {
    int id;
    int level;
} channel_t;
static channel_t g_channels[4];
int channel_level(int index) {
    if (index < 0 || index >= 4) {
        return -1;
    }
    if (g_channels[index].level > 100) {
        return 100;
    }
    return g_channels[index].level;
}
