// ring_buffer.hpp - fixed-size ring buffer, header only.
#pragma once
#include <cstddef>
#include <stdexcept>

template <typename T, std::size_t N>
class RingBuffer {
public:
    /// Adds a value. @throws std::overflow_error when the buffer is full.
    void push(const T& v)
    {
        if (count_ == N) {
            throw std::overflow_error("ring buffer full");
        }
        data_[head_] = v;
        head_ = (head_ + 1u) % N;
        ++count_;
    }
    /// Removes the oldest value. @return false when the buffer is empty.
    bool pop(T& out)
    {
        if (count_ == 0u) {
            return false;
        }
        out = data_[tail_];
        tail_ = (tail_ + 1u) % N;
        --count_;
        return true;
    }
    std::size_t size() const { return count_; }
    bool full() const { return count_ == N; }

private:
    T data_[N] {};
    std::size_t head_ = 0u;
    std::size_t tail_ = 0u;
    std::size_t count_ = 0u;
};
