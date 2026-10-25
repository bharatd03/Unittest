// pt_templates.hpp - generic building blocks of the powertrain controller.
#ifndef PT_TEMPLATES_HPP
#define PT_TEMPLATES_HPP

#include <array>
#include <cstddef>
#include <cstdint>

namespace pt {
namespace detail {

// ------------------------------------------------------------------------------------------ generic helpers
template <typename T>
constexpr T clamp(T value, T lo, T hi)
{
    return value < lo ? lo : (value > hi ? hi : value);
}

template <typename T>
constexpr T abs_value(T value)
{
    return value < T{} ? static_cast<T>(-value) : value;
}

template <typename... B>
constexpr bool all_of(B... flags)
{
    return (... && flags);
}

template <typename... B>
constexpr bool any_of(B... flags)
{
    return (... || flags);
}

template <typename T>
struct Limits;

template <>
struct Limits<std::int16_t> {
    static constexpr std::int32_t lo = -32768;
    static constexpr std::int32_t hi = 32767;
};

template <>
struct Limits<std::int32_t> {
    static constexpr std::int64_t lo = -2147483647LL - 1;
    static constexpr std::int64_t hi = 2147483647LL;
};

template <>
struct Limits<std::uint16_t> {
    static constexpr std::int32_t lo = 0;
    static constexpr std::int32_t hi = 65535;
};

template <typename T, typename W>
constexpr T saturate(W wide)
{
    return static_cast<T>(wide < static_cast<W>(Limits<T>::lo) ? static_cast<W>(Limits<T>::lo)
                          : (wide > static_cast<W>(Limits<T>::hi) ? static_cast<W>(Limits<T>::hi) : wide));
}

// ------------------------------------------------------------------------------------------ filters
template <typename T, unsigned Shift>
class Ema {
public:
    T update(T sample)
    {
        if (!primed_) {
            acc_ = static_cast<std::int32_t>(sample) * (1 << Shift);
            primed_ = true;
        } else {
            acc_ += static_cast<std::int32_t>(sample) - (acc_ >> Shift);
        }
        return static_cast<T>(acc_ >> Shift);
    }
    void reset() { primed_ = false; acc_ = 0; }
    bool primed() const { return primed_; }

private:
    std::int32_t acc_ = 0;
    bool primed_ = false;
};

template <typename T>
class RateLimiter {
public:
    constexpr RateLimiter(T up, T down) : up_(up), down_(down) {}
    T step(T target)
    {
        if (target > out_ + up_) {
            out_ = static_cast<T>(out_ + up_);
        } else if (target < out_ - down_) {
            out_ = static_cast<T>(out_ - down_);
        } else {
            out_ = target;
        }
        return out_;
    }
    void set(T value) { out_ = value; }
    T value() const { return out_; }

private:
    T up_;
    T down_;
    T out_{};
};

template <typename T, std::size_t N>
class RingBuffer {
public:
    bool push(const T& item)
    {
        if (count_ == N) {
            ++overflows_;
            head_ = (head_ + 1) % N;
            --count_;
        }
        items_[(head_ + count_) % N] = item;
        ++count_;
        return overflows_ == 0;
    }
    bool pop(T& out)
    {
        if (count_ == 0) {
            return false;
        }
        out = items_[head_];
        head_ = (head_ + 1) % N;
        --count_;
        return true;
    }
    std::size_t size() const { return count_; }
    std::size_t overflows() const { return overflows_; }
    template <typename F>
    std::size_t count_if(F predicate) const
    {
        std::size_t n = 0;
        for (std::size_t i = 0; i < count_; ++i) {
            if (predicate(items_[(head_ + i) % N])) {
                ++n;
            }
        }
        return n;
    }
    void clear() { head_ = 0; count_ = 0; overflows_ = 0; }

private:
    std::array<T, N> items_{};
    std::size_t head_ = 0;
    std::size_t count_ = 0;
    std::size_t overflows_ = 0;
};

template <typename T>
class Pid {
public:
    constexpr Pid(std::int32_t kp, std::int32_t ki, std::int32_t kd, T lo, T hi) : kp_(kp), ki_(ki), kd_(kd), lo_(lo), hi_(hi) {}
    T step(T setpoint, T measured, bool hold)
    {
        const std::int32_t error = static_cast<std::int32_t>(setpoint) - measured;
        const std::int32_t derivative = first_ ? 0 : error - last_error_;
        first_ = false;
        last_error_ = error;
        std::int32_t out = (kp_ * error + integral_ + kd_ * derivative) / 256;
        if (hold) {
            return clamp(static_cast<T>(clamp<std::int32_t>(out, lo_, hi_)), lo_, hi_);
        }
        if (out > hi_) {
            out = hi_;
            if (error < 0) {
                integral_ += ki_ * error;
            }
        } else if (out < lo_) {
            out = lo_;
            if (error > 0) {
                integral_ += ki_ * error;
            }
        } else {
            integral_ += ki_ * error;
        }
        return static_cast<T>(out);
    }
    void reset() { integral_ = 0; last_error_ = 0; first_ = true; }
    std::int32_t integral() const { return integral_; }

private:
    std::int32_t kp_;
    std::int32_t ki_;
    std::int32_t kd_;
    T lo_;
    T hi_;
    std::int32_t integral_ = 0;
    std::int32_t last_error_ = 0;
    bool first_ = true;
};

}  // namespace detail
}  // namespace pt

#endif
