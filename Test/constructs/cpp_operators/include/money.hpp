// money.hpp
#pragma once
struct Money {
    long cents = 0;
};
/// sum of two amounts, limited to 1 000 000 cents
Money operator+(const Money& a, const Money& b);
bool operator==(const Money& a, const Money& b);
