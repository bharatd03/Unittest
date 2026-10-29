// mode.cpp - a function of a namespace that returns an enumeration of that namespace
#include "mode.hpp"
namespace car {
Mode select_mode(Mode current, unsigned button, bool stopped) {
    if (button == 2u) {
        return stopped ? Mode::Service : current;
    }
    if (button == 1u) {
        return current == Mode::Comfort ? Mode::Sport : Mode::Comfort;
    }
    return current;
}
}
