// codec.cpp
#include "codec.hpp"
#include "hw.hpp"
int encode(std::uint8_t small, std::uint16_t wide) {
    int done = 0;
    if (hw::write(small)) {
        ++done;
    }
    if (hw::write(wide)) {
        ++done;
    }
    return done;
}
