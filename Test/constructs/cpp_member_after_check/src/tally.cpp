// tally.cpp
#include "tally.hpp"
int Tally::bump() {
    int before = count_;
    if (count_ < 100) {
        ++count_;
    }
    return before;
}
