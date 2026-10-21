#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

#include "boards/stackchan/touch_gesture.h"

namespace {

struct Emitted {
    TouchGestureTracker::Kind kind;
    uint64_t duration_ms;
    uint64_t at_ms;
};

struct Touch {
    int64_t start_ms;
    int64_t end_ms;
};

// Samples a physical touch timeline at 10 Hz, with the first sample at
// `phase_ms`, and returns every event the tracker emits.
std::vector<Emitted> Simulate(const std::vector<Touch>& touches, int phase_ms, int64_t total_ms = 6000) {
    TouchGestureTracker tracker;
    std::vector<Emitted> out;
    for (int64_t t = phase_ms; t < total_ms; t += 100) {
        bool pressed = false;
        for (const Touch& touch : touches) {
            if (t >= touch.start_ms && t < touch.end_ms) {
                pressed = true;
            }
        }
        auto r = tracker.Update((uint64_t)t * 1000ULL, pressed);
        if (r.kind != TouchGestureTracker::Kind::kNone) {
            out.push_back({r.kind, r.duration_ms, (uint64_t)t});
        }
    }
    return out;
}

int CountKind(const std::vector<Emitted>& events, TouchGestureTracker::Kind kind) {
    int n = 0;
    for (const Emitted& e : events) {
        if (e.kind == kind) {
            n++;
        }
    }
    return n;
}

}  // namespace

TEST(TouchGesture, IdleEmitsNothing) {
    EXPECT_TRUE(Simulate({}, 0).empty());
}

TEST(TouchGesture, SingleQuickTapIsOneTap) {
    for (int phase : {0, 25, 50, 75}) {
        auto events = Simulate({{1000, 1100}}, phase);
        ASSERT_EQ(events.size(), 1u) << "phase " << phase;
        EXPECT_EQ(events[0].kind, TouchGestureTracker::Kind::kTap) << "phase " << phase;
        EXPECT_LT(events[0].duration_ms, 400u) << "phase " << phase;
    }
}

TEST(TouchGesture, LongHoldIsOneStroke) {
    for (int phase : {0, 25, 50, 75}) {
        auto events = Simulate({{1000, 2000}}, phase);
        ASSERT_EQ(events.size(), 1u) << "phase " << phase;
        EXPECT_EQ(events[0].kind, TouchGestureTracker::Kind::kStroke) << "phase " << phase;
        EXPECT_GE(events[0].duration_ms, 400u) << "phase " << phase;
    }
}

TEST(TouchGesture, SingleSampleBlipInsideStrokeDoesNotSplit) {
    // 1-sample (100 ms) untouched gap in the middle of a long hold.
    for (int phase : {0, 25, 50, 75}) {
        auto events = Simulate({{1000, 1800}, {1900, 2700}}, phase);
        ASSERT_EQ(events.size(), 1u) << "phase " << phase;
        EXPECT_EQ(events[0].kind, TouchGestureTracker::Kind::kStroke) << "phase " << phase;
    }
}

// The tap count must not depend on the gap between two taps or on the
// sampling phase. The old cooldown was measured from the debounce-confirmed
// release, which swallowed the second tap for gaps of roughly 400-650 ms.
TEST(TouchGesture, TwoTapsAreBothRecognizedForGapsOf300To600Ms) {
    for (int gap_ms : {300, 400, 500, 600, 700, 1000}) {
        for (int phase : {0, 10, 25, 40, 50, 60, 75, 90}) {
            const int64_t first_start = 1000;
            const int64_t first_end = first_start + 100;
            const int64_t second_start = first_end + gap_ms;
            auto events = Simulate({{first_start, first_end}, {second_start, second_start + 100}}, phase);
            EXPECT_EQ(CountKind(events, TouchGestureTracker::Kind::kTap), 2)
                << "gap " << gap_ms << " ms, phase " << phase;
            EXPECT_EQ(CountKind(events, TouchGestureTracker::Kind::kStroke), 0)
                << "gap " << gap_ms << " ms, phase " << phase;
        }
    }
}

TEST(TouchGesture, TwoTapsWithLongerPressesAreBothRecognized) {
    for (int gap_ms : {300, 400, 500, 600}) {
        for (int phase : {0, 25, 50, 75}) {
            const int64_t second_start = 1000 + 250 + gap_ms;
            auto events = Simulate({{1000, 1250}, {second_start, second_start + 250}}, phase);
            EXPECT_EQ(CountKind(events, TouchGestureTracker::Kind::kTap), 2)
                << "gap " << gap_ms << " ms, phase " << phase;
        }
    }
}

TEST(TouchGesture, SixRapidTapsAreAllRecognized) {
    for (int gap_ms : {300, 400, 500, 600}) {
        for (int phase : {0, 25, 50, 75}) {
            std::vector<Touch> touches;
            int64_t t = 500;
            for (int i = 0; i < 6; i++) {
                touches.push_back({t, t + 100});
                t += 100 + gap_ms;
            }
            auto events = Simulate(touches, phase, t + 2000);
            EXPECT_EQ(CountKind(events, TouchGestureTracker::Kind::kTap), 6)
                << "gap " << gap_ms << " ms, phase " << phase;
        }
    }
}

TEST(TouchGesture, TapAfterStrokeIsRecognizedOnceCooldownHasPassed) {
    for (int phase : {0, 25, 50, 75}) {
        auto events = Simulate({{1000, 1800}, {2600, 2700}}, phase);
        EXPECT_EQ(CountKind(events, TouchGestureTracker::Kind::kStroke), 1) << "phase " << phase;
        EXPECT_EQ(CountKind(events, TouchGestureTracker::Kind::kTap), 1) << "phase " << phase;
    }
}

TEST(TouchGesture, PressStartedIsReportedOnNewPress) {
    TouchGestureTracker tracker;
    auto r = tracker.Update(100000, true);
    EXPECT_TRUE(r.press_started);
    EXPECT_EQ(r.kind, TouchGestureTracker::Kind::kNone);
}
