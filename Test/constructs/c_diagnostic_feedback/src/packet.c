#include "packet.h"
int packet_probe(const unsigned char *payload, int length)
{
    if (!payload || length <= 0) return -1;
    if (payload[0] == 19) return 7;
    return 0;
}
