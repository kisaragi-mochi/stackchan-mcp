#pragma once

// Head-touch gesture classifier for the Si12T (CH1..CH3 = front/middle/back).
// Pure C++ (no ESP-IDF headers) so it can be unit-tested on the host.
//
// Feed one sample every TouchGestureConfig::tick_ms with the 0..3 level of
// each channel. The tracker:
//   - ignores everything for `boot_grace_ms` after Begin() (Si12T fast
//     calibration window) and keeps channels that are already "touched" at the
//     end of the window unusable until they read 0 for `unmask_zero_ms`;
//   - watches for channels that stay non-zero for `stuck_ms`: those are
//     reported in `recal_mask` (caller resets the chip reference), dropped
//     from the decision, and re-admitted after `unmask_zero_ms` of zeros;
//   - classifies one press (OR of usable channels) as
//       STROKE: the finger left a zone it started on AND reached a zone it did
//               not start on (moved across zones),
//       TAP:    no movement, `tap_min_ms`..`tap_max_ms` hold, and either some
//               channel reached level M (2) or more, or the press stayed on
//               for `tap_low_min_ms` (a lone short level-L blip is dropped),
//       none:   stationary hold longer than `tap_max_ms` (e.g. a stuck
//               channel or a resting hand), or a short L-only blip.
//   - NotifySettle() is called when charging or the backlight switches. The
//     capacitive readings wobble for up to ~1 s afterwards, but only as a
//     stationary blip on a single channel; a finger sliding across zones is
//     never mistaken for one. So for `settle_ms`:
//       * a press that is already on keeps being tracked as usual;
//       * a press that starts inside the window may only end as a STROKE (it
//         moved across zones); a stationary one is dropped, never a TAP;
//     and when the window ends the caller is asked to reset the chip reference
//     of all channels (`recal_mask`). That reset waits until a finger that is
//     really on the pad (started before the switch, or already sliding) lets
//     go; channels that read non-zero at the reset stay masked until they read
//     0 for `settle_unmask_zero_ms`.

#include <cstdint>

namespace stackchan {
namespace touch {

struct TouchGestureConfig {
    uint32_t tick_ms = 50;
    uint32_t boot_grace_ms = 10000;
    uint32_t stuck_ms = 15000;
    uint32_t unmask_zero_ms = 1000;
    uint32_t release_confirm_ticks = 3;     // 150 ms of zeros end a press
    uint32_t move_zone_confirm_ticks = 2;   // new zone must persist (100 ms)
    uint32_t move_release_confirm_ticks = 2;  // start zone must stay off (100 ms)
    uint32_t tap_min_ms = 40;
    uint32_t tap_max_ms = 500;
    uint32_t tap_low_min_ms = 300;          // L-only (peak level 1) needs this long
    uint8_t tap_strong_level = 2;           // a peak at this level or above counts at once
    uint32_t settle_ms = 1000;              // tap-proof window after a power/backlight switch
    uint32_t settle_unmask_zero_ms = 300;   // zeros needed to unmask after that reset
    uint32_t cooldown_ms = 300;
};

enum class TouchGestureEvent : uint8_t {
    NONE = 0,
    TAP,
    STROKE,
};

struct TouchGestureOutput {
    TouchGestureEvent event = TouchGestureEvent::NONE;  // emitted on release
    uint32_t duration_ms = 0;       // TAP: hold length. STROKE: press length.
    bool moved_now = false;         // finger movement just confirmed (STROKE in progress)
    bool ended_without_event = false;  // a press ended and was not a TAP/STROKE
    uint8_t recal_mask = 0;         // bit i = CH(i+1): reset the chip reference now
    uint8_t start_zones = 0;        // zones touched at press start (valid with event)
    uint8_t peak_level = 0;         // highest level seen on a usable channel in the press
    bool dropped_weak = false;      // a short L-only press was discarded (log it)
    bool settle_reset = false;      // recal_mask is for the post-switch reference reset
};

class TouchGestureTracker {
public:
    static constexpr int kChannels = 3;

    explicit TouchGestureTracker(const TouchGestureConfig& config = TouchGestureConfig())
        : cfg_(config) {}

    // Restart (e.g. after the chip was re-initialised). Starts the boot grace.
    void Begin(uint32_t now_ms) {
        const TouchGestureConfig cfg = cfg_;
        *this = TouchGestureTracker(cfg);
        begin_ms_ = now_ms;
        started_ = true;
    }

    uint8_t masked_channels() const { return masked_; }
    uint32_t recalibrations() const { return recalibrations_; }
    uint32_t settle_resets() const { return settle_resets_; }
    bool in_settle() const { return settle_active_; }

    // Charging / backlight just changed: power rails move and the capacitive
    // readings wobble (see the header comment for what is and is not accepted).
    void NotifySettle(uint32_t now_ms) {
        if (!started_ || in_grace_) return;
        settle_active_ = true;
        settle_until_ms_ = now_ms + cfg_.settle_ms;
    }
    bool in_boot_grace() const { return in_grace_; }
    bool pressed() const { return pressed_; }

    TouchGestureOutput Update(uint32_t now_ms, const uint8_t levels[kChannels]) {
        TouchGestureOutput out;
        if (!started_) {
            Begin(now_ms);
        }

        uint8_t raw = 0;  // bit i: CH(i+1) reads non-zero
        for (int i = 0; i < kChannels; i++) {
            if (levels[i] != 0) raw |= static_cast<uint8_t>(1u << i);
        }

        if (in_grace_) {
            if (now_ms - begin_ms_ < cfg_.boot_grace_ms) {
                return out;
            }
            in_grace_ = false;
            // Anything already touched when the window closes is a leftover of
            // the boot calibration, not a finger we saw arrive.
            masked_ = raw;
            for (int i = 0; i < kChannels; i++) {
                nonzero_ms_[i] = 0;
                zero_ms_[i] = 0;
                unmask_need_ms_[i] = cfg_.unmask_zero_ms;
            }
            last_tick_ms_ = now_ms;
        }

        const uint32_t dt = last_tick_ms_ == 0 ? cfg_.tick_ms : (now_ms - last_tick_ms_);
        last_tick_ms_ = now_ms;

        const bool in_window = settle_active_ && now_ms < settle_until_ms_;
        if (settle_active_ && !in_window &&
            (!pressed_ || (press_in_settle_ && !moved_ &&
                           now_ms - press_start_ms_ >= cfg_.tap_max_ms))) {
            // Window over. A press that began inside it and has sat still for
            // longer than any tap is a lingering blip; a real finger (began
            // earlier, already sliding, or still young) is left alone and the
            // reset happens once it lets go.
            settle_active_ = false;
            settle_resets_++;
            pressed_ = false;
            press_dirty_ = false;
            moved_ = false;
            zero_ticks_ = 0;
            cooldown_valid_ = false;
            const uint8_t fresh = static_cast<uint8_t>(raw & 0x07 & ~masked_);
            masked_ |= fresh;
            for (int i = 0; i < kChannels; i++) {
                nonzero_ms_[i] = 0;
                zero_ms_[i] = 0;
                if (fresh & (1u << i)) unmask_need_ms_[i] = cfg_.settle_unmask_zero_ms;
            }
            out.recal_mask = 0x07;
            out.settle_reset = true;
            return out;
        }

        // ---- stuck watch / unmask -------------------------------------
        for (int i = 0; i < kChannels; i++) {
            const uint8_t bit = static_cast<uint8_t>(1u << i);
            if (raw & bit) {
                zero_ms_[i] = 0;
                nonzero_ms_[i] += dt;
                if (nonzero_ms_[i] >= cfg_.stuck_ms) {
                    nonzero_ms_[i] = 0;
                    masked_ |= bit;
                    unmask_need_ms_[i] = cfg_.unmask_zero_ms;
                    out.recal_mask |= bit;
                    recalibrations_++;
                    if (pressed_) press_dirty_ = true;
                }
            } else {
                nonzero_ms_[i] = 0;
                zero_ms_[i] += dt;
                if ((masked_ & bit) && zero_ms_[i] >= unmask_need_ms_[i]) {
                    masked_ &= static_cast<uint8_t>(~bit);
                }
            }
        }

        const uint8_t active = raw & static_cast<uint8_t>(~masked_) & 0x07;

        // ---- press tracking ---------------------------------------------
        if (!pressed_) {
            if (active == 0) {
                return out;
            }
            pressed_ = true;
            press_start_ms_ = now_ms;
            last_active_ms_ = now_ms;
            start_zones_ = active;
            press_in_settle_ = in_window;
            peak_level_ = 0;
            for (int i = 0; i < kChannels; i++) {
                if ((active & (1u << i)) && levels[i] > peak_level_) peak_level_ = levels[i];
            }
            seen_new_ = 0;
            vacated_ = 0;
            moved_ = false;
            zero_ticks_ = 0;
            for (int i = 0; i < kChannels; i++) {
                new_ticks_[i] = 0;
                off_ticks_[i] = 0;
            }
            // A press that begins inside the cooldown is not ours to report.
            press_dirty_ = (cooldown_valid_ && now_ms - cooldown_from_ms_ < cfg_.cooldown_ms);
            return out;
        }

        if (active != 0) {
            zero_ticks_ = 0;
            last_active_ms_ = now_ms;
        } else {
            zero_ticks_++;
        }

        for (int i = 0; i < kChannels; i++) {
            if ((active & (1u << i)) && levels[i] > peak_level_) peak_level_ = levels[i];
        }

        // Movement: left a start zone, reached a zone that was not a start zone.
        // Only ticks with the pad still touched count, so lifting the finger
        // off (every zone goes quiet) is never mistaken for sliding away.
        for (int i = 0; active != 0 && i < kChannels; i++) {
            const uint8_t bit = static_cast<uint8_t>(1u << i);
            const bool on = (active & bit) != 0;
            if (start_zones_ & bit) {
                off_ticks_[i] = on ? 0 : off_ticks_[i] + 1;
                if (off_ticks_[i] >= cfg_.move_release_confirm_ticks) vacated_ |= bit;
            } else {
                new_ticks_[i] = on ? new_ticks_[i] + 1 : 0;
                if (new_ticks_[i] >= cfg_.move_zone_confirm_ticks) seen_new_ |= bit;
            }
        }
        if (!moved_ && !press_dirty_ && vacated_ != 0 && seen_new_ != 0) {
            moved_ = true;
            out.moved_now = true;
        }

        if (zero_ticks_ < cfg_.release_confirm_ticks) {
            return out;
        }

        // ---- release ------------------------------------------------------
        pressed_ = false;
        const uint32_t duration = last_active_ms_ - press_start_ms_ + cfg_.tick_ms;
        cooldown_valid_ = true;
        cooldown_from_ms_ = last_active_ms_;
        if (!press_dirty_) {
            if (moved_) {
                out.event = TouchGestureEvent::STROKE;
                out.duration_ms = duration;
                out.start_zones = start_zones_;
            } else if (!press_in_settle_ && duration >= cfg_.tap_min_ms &&
                       duration <= cfg_.tap_max_ms) {
                if (peak_level_ >= cfg_.tap_strong_level || duration >= cfg_.tap_low_min_ms) {
                    out.event = TouchGestureEvent::TAP;
                    out.duration_ms = duration;
                    out.start_zones = start_zones_;
                } else {
                    out.dropped_weak = true;
                }
            }
        }
        out.peak_level = peak_level_;
        // moved_now never fires without a matching STROKE unless the press was
        // dirty, and dirty presses suppress movement.
        if (out.event == TouchGestureEvent::NONE) {
            out.ended_without_event = true;
            out.duration_ms = duration;
        }
        press_dirty_ = false;
        return out;
    }

private:
    TouchGestureConfig cfg_;
    bool started_ = false;
    bool in_grace_ = true;
    uint32_t begin_ms_ = 0;
    uint32_t last_tick_ms_ = 0;

    uint8_t masked_ = 0;
    uint32_t recalibrations_ = 0;
    uint32_t settle_resets_ = 0;
    bool settle_active_ = false;
    uint32_t settle_until_ms_ = 0;
    uint8_t peak_level_ = 0;
    uint32_t nonzero_ms_[kChannels] = {0, 0, 0};
    uint32_t zero_ms_[kChannels] = {0, 0, 0};
    uint32_t unmask_need_ms_[kChannels] = {1000, 1000, 1000};
    bool press_in_settle_ = false;

    bool pressed_ = false;
    bool press_dirty_ = false;
    bool moved_ = false;
    uint32_t press_start_ms_ = 0;
    uint32_t last_active_ms_ = 0;
    uint32_t zero_ticks_ = 0;
    uint8_t start_zones_ = 0;
    uint8_t seen_new_ = 0;
    uint8_t vacated_ = 0;
    uint32_t new_ticks_[kChannels] = {0, 0, 0};
    uint32_t off_ticks_[kChannels] = {0, 0, 0};

    bool cooldown_valid_ = false;
    uint32_t cooldown_from_ms_ = 0;
};

}  // namespace touch
}  // namespace stackchan
