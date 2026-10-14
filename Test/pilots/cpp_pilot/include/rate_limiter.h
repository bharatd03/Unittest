// Pilot SUT (C++): a token-bucket rate limiter.
#ifndef RATE_LIMITER_H
#define RATE_LIMITER_H

class RateLimiter {
public:
    /** @param capacity tokens per window, must be > 0
     *  @param window_ms window length in milliseconds, must be > 0 */
    RateLimiter(int capacity, long long window_ms);
    /** @return true when a request may pass now, false when the limit is reached */
    bool allow();
    /** @return tokens left in the current window */
    int remaining() const;

private:
    int capacity_;
    long long window_ms_;
    long long window_start_;
    int used_;
};

/** @return true when the burst is valid: 1..capacity and not in maintenance mode */
bool valid_burst(int burst, int capacity, bool maintenance);

#endif
