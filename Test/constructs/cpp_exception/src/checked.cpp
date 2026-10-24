// checked.cpp
#include <stdexcept>
#include "checked.hpp"
int checked_div(int a, int b) {
    if (b == 0) {
        throw std::invalid_argument("division by zero");
    }
    return a / b;
}
