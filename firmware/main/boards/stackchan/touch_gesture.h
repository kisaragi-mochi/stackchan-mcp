// Head-touch gesture tracker (debounce + TAP/STROKE classification).
//
// Pure C++ with no ESP-IDF dependency so the timing can be unit-tested on the
// host (firmware/host_test/test_touch_gesture.cc). The board code feeds it
// one sample per poll and reacts to the returned event.
//
// Timing model (10 Hz polling):
//   - press confirms on the first touched sample; release needs
//     RELEASE_CONFIRM_SAMPLES consecutive untouched samples, so Si12T
//     recalibration blips and finger glides do not cut a stroke short.
//   - gesture duration is measured between the first samples of the press
//     and release states, not the debounce-confirmed edges.
//   - the post-reaction cooldown is measured from the same physical release
//     (first untouched sample) in every path, so the number of taps accepted
//     does not depend on the sampling phase.
#pragma once

#include <cstdint>

class TouchGestureTracker {
public:
    static constexpr int STROKE_MIN_MS = 400;
    static constexpr int COOLDOWN_MS = 250;
    static constexpr int PRESS_CONFIRM_SAMPLES = 1;
    static constexpr int RELEASE_CONFIRM_SAMPLES = 4;

    enum class Kind : uint8_t { kNone, kTap, kStroke };

    struct Result {
        Kind kind = Kind::kNone;
        uint64_t duration_ms = 0;
        // True when a new press began on this sample (including a press
        // suppressed by the cooldown). The caller snapshots the sensor state
        // for logging *after* handling `kind`, because a tap emitted on the
        // same sample still belongs to the previous press.
        bool press_started = false;
    };

    Result Update(uint64_t now_us, bool any_pressed) {
        Result result;
        if (any_pressed == pending_) {
            pending_count_++;
        } else {
            if (any_pressed && pressed_ && pending_count_ >= 2) {
                // Re-press after a >=2-sample gap while the release was still
                // awaiting its confirmation. A 1-sample gap is recalibration /
                // finger-glide noise (stay sticky), but a longer gap means the
                // user really let go: without this branch two quick taps merge
                // into one long press and classify as a stroke. A tolerated
                // blip inside a real (>=STROKE_MIN_MS) stroke still merges.
                const uint64_t segment_ms = (pending_since_us_ - press_start_us_) / 1000ULL;
                if (segment_ms < (uint64_t)STROKE_MIN_MS) {
                    // Judge against the cooldown as it stood before this
                    // emit; the fresh cooldown must not gate the new press.
                    const bool in_old_cooldown = now_us < cooldown_until_us_;
                    if (!suppressed_ && !in_old_cooldown) {
                        result.kind = Kind::kTap;
                        result.duration_ms = segment_ms;
                        cooldown_until_us_ = pending_since_us_ + CooldownUs();
                    }
                    suppressed_ = in_old_cooldown;
                    press_start_us_ = now_us;
                    result.press_started = true;
                }
            }
            pending_count_ = 1;
            pending_ = any_pressed;
            pending_since_us_ = now_us;
        }

        const int needed = pending_ ? PRESS_CONFIRM_SAMPLES : RELEASE_CONFIRM_SAMPLES;
        if (pending_count_ < needed || pending_ == pressed_) {
            return result;
        }

        if (pending_) {
            result.press_started = true;
            suppressed_ = now_us < cooldown_until_us_;
            pressed_ = true;
            press_start_us_ = pending_since_us_;
            return result;
        }

        pressed_ = false;
        if (suppressed_) {
            suppressed_ = false;
            return result;
        }
        result.duration_ms = (pending_since_us_ - press_start_us_) / 1000ULL;
        result.kind =
            result.duration_ms >= (uint64_t)STROKE_MIN_MS ? Kind::kStroke : Kind::kTap;
        cooldown_until_us_ = pending_since_us_ + CooldownUs();
        return result;
    }

private:
    static constexpr uint64_t CooldownUs() { return (uint64_t)COOLDOWN_MS * 1000ULL; }

    bool pressed_ = false;   // debounced state
    bool pending_ = false;   // candidate state awaiting confirmation
    int pending_count_ = 0;  // consecutive samples matching pending_
    uint64_t press_start_us_ = 0;
    uint64_t pending_since_us_ = 0;  // first sample of the current pending state
    bool suppressed_ = false;        // press began during cooldown
    uint64_t cooldown_until_us_ = 0;
};
