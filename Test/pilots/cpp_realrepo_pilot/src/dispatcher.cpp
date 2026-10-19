// dispatcher.cpp - the SUT: calls framework services of namespace fw.
#include "fw/port.hpp"

namespace fw {

int dispatch(Evt const * const e)
{
    if (e == nullptr) {
        onError("dispatcher", 1);
        return -1;
    }
    if (e->sig > 10U) {
        return post(e, 32U) ? 1 : 0;
    }
    return 2;
}

} // namespace fw
