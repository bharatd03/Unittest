// tally.hpp
#pragma once
class Tally {
public:
    /// adds one to the count; @return the count before
    int bump();
private:
    int count_ = 0;
};
