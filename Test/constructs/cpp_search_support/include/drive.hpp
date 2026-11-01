#pragma once
class Drive {
public:
    explicit Drive(int initial) : value_(initial) {}
    int step(int delta);
private:
    int value_;
};
