// supervisor.cpp
#include "mode.hpp"
#include "board.hpp"
namespace app {
int supervise(Mode mode) {
    if (mode == Mode::Service) {
        return 2;
    }
    if (board::g_supply_mv < 9000) {
        return -1;
    }
    if (board::read_temp() > 90) {
        return -2;
    }
    return 0;
}
}
