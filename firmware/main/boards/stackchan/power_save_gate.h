// Decides when the automatic dim / power-off timer (PowerSaveTimer) runs.
//
// Header-only and free of ESP-IDF dependencies so the decision can be
// covered by host tests (firmware/host_test/test_power_save_gate.cc).
#pragma once

namespace stackchan::power {

// The timer runs on battery (discharging). On external power it runs only
// when the owner opted in with external_power_auto_off.
inline bool ShouldRunPowerSaveTimer(bool discharging,
                                    bool external_power_auto_off) {
    return discharging || external_power_auto_off;
}

// Remembers the timer state that was last applied so the board only calls
// PowerSaveTimer::SetEnabled() when the desired state changes. It must be
// constructed with the state the timer actually starts in; a mismatch here
// is what left the timer running on USB power when booting without a
// battery transition.
class PowerSaveGate {
public:
    explicit PowerSaveGate(bool timer_enabled) : applied_(timer_enabled) {}

    // Returns true and stores the new state in *enable when the timer must
    // be switched; returns false when it is already in the desired state.
    bool Update(bool discharging, bool external_power_auto_off,
                bool* enable) {
        bool desired =
            ShouldRunPowerSaveTimer(discharging, external_power_auto_off);
        if (desired == applied_) {
            return false;
        }
        applied_ = desired;
        *enable = desired;
        return true;
    }

    bool applied() const { return applied_; }

private:
    bool applied_;
};

}  // namespace stackchan::power
