// moving_average.hpp
#pragma once
template <typename T, int N>
class MovingAverage {
public:
    void add(T v);
    /// @return the mean of the samples added, T{} before the first one
    T value() const;
private:
    T buf_[N]{};
    int count_ = 0;
    int head_ = 0;
};
