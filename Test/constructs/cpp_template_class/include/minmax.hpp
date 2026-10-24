// minmax.hpp
#pragma once
template <typename T>
class MinMax {
public:
    void add(T v) {
        if (count_ == 0 || v < min_) { min_ = v; }
        if (count_ == 0 || v > max_) { max_ = v; }
        ++count_;
    }
    T span() const { return count_ ? max_ - min_ : T{}; }
private:
    T min_{};
    T max_{};
    int count_ = 0;
};
