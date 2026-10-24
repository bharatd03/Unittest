// money.cpp
#include "money.hpp"
Money operator+(const Money& a, const Money& b) {
    Money r;
    r.cents = a.cents + b.cents;
    if (r.cents > 1000000) {
        r.cents = 1000000;
    }
    return r;
}
bool operator==(const Money& a, const Money& b) {
    return a.cents == b.cents;
}
