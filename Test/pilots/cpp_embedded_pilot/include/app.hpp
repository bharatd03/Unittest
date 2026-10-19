// app.hpp - mode logic of the firmware.
#pragma once
namespace app {
/// One pass of the main loop. @return 0 idle, 1 run, 2 fault.
int step(int mode, bool button, bool fault);
}
