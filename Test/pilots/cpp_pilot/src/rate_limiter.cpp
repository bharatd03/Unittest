#include "rate_limiter.h"
#include "clock.h"

RateLimiter::RateLimiter(int capacity, long long window_ms)
    : capacity_(capacity > 0 ? capacity : 1), window_ms_(window_ms), window_start_(now_ms()), used_(0)
{
}

bool RateLimiter::allow()
{
    long long now = now_ms();
    if (now - window_start_ >= window_ms_) {
        window_start_ = now;
        used_ = 0;
    }
    if (used_ < capacity_) {
        ++used_;
        return true;
    }
    return false;
}

int RateLimiter::remaining() const
{
    return capacity_ - used_;
}

bool valid_burst(int burst, int capacity, bool maintenance)
{
    return burst > 0 && burst <= capacity && !maintenance;
}
