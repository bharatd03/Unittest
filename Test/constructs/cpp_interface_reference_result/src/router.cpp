// router.cpp
#include "router.hpp"
int Router::port_for(int dest) const {
    const Route& r = table_.lookup(dest);
    if (!r.up) {
        return -1;
    }
    return r.port;
}
