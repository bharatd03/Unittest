// app_main.cpp - entry point with the endless main loop.
#include "app.hpp"

namespace app {
int step(int mode, bool button, bool fault)
{
    if (fault) {
        return 2;
    }
    if (button) {
        return mode == 0 ? 1 : 0;
    }
    return mode;
}
}  // namespace app

int main()
{
    int mode = 0;
    for (;;) {
        mode = app::step(mode, false, false);
    }
}
