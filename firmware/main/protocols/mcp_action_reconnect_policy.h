#ifndef MCP_ACTION_RECONNECT_POLICY_H
#define MCP_ACTION_RECONNECT_POLICY_H

#include <algorithm>
#include <cstdint>

// Pure host-testable retry policy for the optional Kimito action transport.
// Connection state and timers remain in McpActionClient; this class only owns
// the deterministic exponential-backoff invariant.
class McpActionReconnectPolicy {
public:
    static constexpr uint32_t kInitialDelayMs = 5000;
    static constexpr uint32_t kMaximumDelayMs = 60000;

    uint32_t ConsumeDelayMs() {
        const uint32_t delay = next_delay_ms_;
        next_delay_ms_ = std::min(next_delay_ms_ * 2, kMaximumDelayMs);
        return delay;
    }

    void Reset() { next_delay_ms_ = kInitialDelayMs; }
    uint32_t PeekDelayMs() const { return next_delay_ms_; }

private:
    uint32_t next_delay_ms_ = kInitialDelayMs;
};

#endif
