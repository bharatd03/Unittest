// moving_average.cpp
#include "moving_average.hpp"
template <typename T, int N>
void MovingAverage<T, N>::add(T v) {
    buf_[head_] = v;
    head_ = (head_ + 1) % N;
    if (count_ < N) {
        ++count_;
    }
}
template <typename T, int N>
T MovingAverage<T, N>::value() const {
    if (count_ == 0) {
        return T{};
    }
    T sum{};
    for (int i = 0; i < count_; ++i) {
        sum += buf_[i];
    }
    return sum / count_;
}
template class MovingAverage<int, 4>;
