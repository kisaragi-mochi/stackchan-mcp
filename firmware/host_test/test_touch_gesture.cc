#include "boards/stackchan/touch_gesture.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <iomanip>
#include <vector>

namespace {

using stackchan::touch::TouchGestureConfig;
using stackchan::touch::TouchGestureEvent;
using stackchan::touch::TouchGestureOutput;
using stackchan::touch::TouchGestureTracker;

constexpr uint32_t kTick = 50;

// Decode a Si12T Output1 byte into CH1..CH3 levels.
void Decode(uint8_t raw, uint8_t levels[3]) {
    levels[0] = (raw >> 0) & 0x3;
    levels[1] = (raw >> 2) & 0x3;
    levels[2] = (raw >> 4) & 0x3;
}

struct Sim {
    TouchGestureTracker tracker;
    uint32_t now = 0;
    int taps = 0;
    int strokes = 0;
    int moved = 0;
    int recal_requests = 0;
    uint8_t recal_union = 0;
    uint32_t last_stroke_ms = 0;

    Sim() { tracker.Begin(0); }

    void Feed(const uint8_t levels[3]) {
        TouchGestureOutput o = tracker.Update(now, levels);
        if (o.event == TouchGestureEvent::TAP) taps++;
        if (o.event == TouchGestureEvent::STROKE) {
            strokes++;
            last_stroke_ms = o.duration_ms;
        }
        if (o.moved_now) moved++;
        if (o.recal_mask) {
            recal_requests++;
            recal_union |= o.recal_mask;
        }
        now += kTick;
    }

    void Raw(uint8_t raw, uint32_t ms) {
        uint8_t l[3];
        Decode(raw, l);
        for (uint32_t t = 0; t < ms; t += kTick) Feed(l);
    }

    void Zones(int c1, int c2, int c3, uint32_t ms) {
        uint8_t l[3] = {static_cast<uint8_t>(c1), static_cast<uint8_t>(c2),
                        static_cast<uint8_t>(c3)};
        for (uint32_t t = 0; t < ms; t += kTick) Feed(l);
    }

    void Idle(uint32_t ms) { Zones(0, 0, 0, ms); }

    // Skip the boot grace window with the pad untouched.
    void PastGrace() { Idle(10000 + 1000); }
};

// ---- 1. replay of the false positives seen on the device ---------------

TEST(TouchGestureReplay, StuckCh2Ch3WithShortDropsGivesNoEvents) {
    // iso_main.log: out1=0x3C at init, 7-20 s plateaus separated by ~0.4 s zeros,
    // for ~85 s.
    Sim s;
    const uint32_t plateaus_ms[] = {7200, 10100, 11700, 10700, 19900, 7600, 15000};
    for (uint32_t p : plateaus_ms) {
        s.Raw(0x3C, p);
        s.Raw(0x00, 400);
    }
    s.Idle(110000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
    EXPECT_EQ(s.moved, 0);
}

TEST(TouchGestureReplay, Stuck270SecondsGivesNoEventsAndRequestsRecalibration) {
    Sim s;
    s.PastGrace();
    s.Raw(0x38, 270000);
    s.Idle(5000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
    EXPECT_GE(s.recal_requests, 1);
    EXPECT_EQ(s.recal_union & 0x06, 0x06);  // CH2 and CH3
    EXPECT_EQ(s.recal_union & 0x01, 0);
}

TEST(TouchGestureReplay, RecalibrationIsRequestedAfterFifteenSeconds) {
    Sim s;
    s.PastGrace();
    s.Raw(0x3C, 14500);
    EXPECT_EQ(s.recal_requests, 0);
    s.Raw(0x3C, 1000);
    EXPECT_EQ(s.recal_requests, 1);
}

TEST(TouchGestureReplay, FlickeringStuckPatternsGiveNoEvents) {
    // 0HH0 <-> 00H0, 0x3D <-> 0x3C, and levels moving between H/M/L, each
    // plateau shorter than the 15 s stuck limit.
    const struct {
        uint8_t a, b;
        uint32_t a_ms, b_ms;
    } patterns[] = {
        {0x3C, 0x30, 350, 250},
        {0x3D, 0x3C, 500, 300},
        {0x3C, 0x38, 400, 400},
        {0x3C, 0x2C, 400, 400},
    };
    for (const auto& p : patterns) {
        Sim s;
        s.PastGrace();
        for (int i = 0; i < 15; i++) {
            s.Raw(p.a, p.a_ms);
            s.Raw(p.b, p.b_ms);
        }
        s.Idle(2000);
        EXPECT_EQ(s.taps, 0) << std::hex << int(p.a) << "/" << int(p.b);
        EXPECT_EQ(s.strokes, 0) << std::hex << int(p.a) << "/" << int(p.b);
    }
}

TEST(TouchGestureReplay, LongFlickerRecalibratesAndStaysQuietOnceTheChipSettles) {
    // 0x3C <-> 0x30 for 24 s: CH3 hits the 15 s limit and is recalibrated;
    // the chip then reads CH3 = 0 again.
    Sim s;
    s.PastGrace();
    for (int i = 0; i < 25; i++) {
        s.Raw(0x3C, 350);
        s.Raw(0x30, 250);
    }
    EXPECT_GE(s.recal_requests, 1);
    s.Idle(5000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureReplay, StuckThenDroppingChannelsAndComingBackIsNotAStroke) {
    Sim s;
    s.PastGrace();
    s.Raw(0x3C, 3000);
    s.Raw(0x30, 3000);  // CH2 dropped
    s.Raw(0x3C, 3000);  // CH2 back (was a start zone)
    s.Idle(2000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

// ---- 2. boot grace -----------------------------------------------------

TEST(TouchGestureBoot, PressesDuringBootGraceProduceNothing) {
    Sim s;
    s.Zones(0, 3, 0, 100);
    s.Idle(500);
    s.Zones(3, 0, 0, 100);
    s.Zones(0, 0, 3, 100);
    s.Idle(500);
    EXPECT_LT(s.now, 10000u);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureBoot, ChannelTouchedWhenGraceEndsStaysUnusableUntilItReadsZero) {
    Sim s;
    s.Raw(0x3C, 10300);  // CH2/CH3 non-zero through the end of the grace window
    s.Idle(100);         // short drop right after the grace ends: not an event
    s.Raw(0x3C, 200);
    s.Idle(300);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
    EXPECT_EQ(s.tracker.masked_channels(), 0x06);
    s.Idle(1100);
    EXPECT_EQ(s.tracker.masked_channels(), 0);
}

// ---- 3. taps ---------------------------------------------------------------

TEST(TouchGestureTap, EachZoneAndDurationGivesOneTap) {
    const uint32_t durations[] = {60, 100, 200, 400};
    for (int zone = 0; zone < 3; zone++) {
        for (uint32_t d : durations) {
            for (uint32_t phase = 0; phase < kTick; phase += 10) {
                Sim s;
                s.PastGrace();
                s.now += phase;  // sample phase relative to the press
                int c[3] = {0, 0, 0};
                c[zone] = 3;
                s.Zones(c[0], c[1], c[2], d);
                s.Idle(1000);
                EXPECT_EQ(s.taps, 1) << "zone=" << zone << " d=" << d;
                EXPECT_EQ(s.strokes, 0) << "zone=" << zone << " d=" << d;
            }
        }
    }
}

TEST(TouchGestureTap, LowLevelTapCounts) {
    Sim s;
    s.PastGrace();
    s.Raw(0x10, 100);  // CH3 = L
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

TEST(TouchGestureTap, TapSpanningTwoZonesDoesNotMove) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 3, 0, 50);
    s.Zones(0, 3, 2, 100);
    s.Zones(0, 3, 0, 50);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureTap, TwoTapsWithShortGapsAreBothReported) {
    const uint32_t gaps[] = {300, 400, 600};
    for (uint32_t gap : gaps) {
        Sim s;
        s.PastGrace();
        s.Zones(0, 3, 0, 100);
        s.Idle(gap);
        s.Zones(0, 3, 0, 100);
        s.Idle(1000);
        EXPECT_EQ(s.taps, 2) << "gap=" << gap;
        EXPECT_EQ(s.strokes, 0) << "gap=" << gap;
    }
}

TEST(TouchGestureTap, SecondPressInsideCooldownIsIgnored) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 3, 0, 100);
    s.Idle(200);
    s.Zones(0, 3, 0, 100);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

TEST(TouchGestureTap, ReleaseBounceInsideConfirmWindowIsOneTap) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 3, 0, 100);
    s.Idle(100);
    s.Zones(0, 3, 0, 50);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

// ---- 4. strokes -----------------------------------------------------------

TEST(TouchGestureStroke, FrontToBackIsOneStroke) {
    Sim s;
    s.PastGrace();
    s.Zones(3, 0, 0, 150);
    s.Zones(3, 3, 0, 100);
    s.Zones(0, 3, 0, 150);
    s.Zones(0, 3, 3, 100);
    s.Zones(0, 0, 3, 200);
    s.Idle(1000);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.moved, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureStroke, BackToFrontIsOneStroke) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 0, 3, 150);
    s.Zones(0, 3, 3, 100);
    s.Zones(0, 3, 0, 150);
    s.Zones(3, 3, 0, 100);
    s.Zones(3, 0, 0, 200);
    s.Idle(1000);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.moved, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureStroke, SingleStepToNeighbourZoneIsAStroke) {
    Sim s;
    s.PastGrace();
    s.Zones(3, 0, 0, 200);
    s.Zones(0, 3, 0, 300);
    s.Idle(1000);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureStroke, GapOf100msBetweenZonesStillStrokes) {
    Sim s;
    s.PastGrace();
    s.Zones(3, 0, 0, 200);
    s.Idle(100);
    s.Zones(0, 3, 0, 200);
    s.Idle(100);
    s.Zones(0, 0, 3, 200);
    s.Idle(1000);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureStroke, BackAndForthRubbingIsStillOneStroke) {
    Sim s;
    s.PastGrace();
    for (int i = 0; i < 4; i++) {
        s.Zones(3, 0, 0, 150);
        s.Zones(3, 3, 0, 100);
        s.Zones(0, 3, 0, 150);
        s.Zones(0, 3, 3, 100);
        s.Zones(0, 0, 3, 150);
        s.Zones(0, 3, 3, 100);
        s.Zones(0, 3, 0, 150);
        s.Zones(3, 3, 0, 100);
    }
    s.Idle(1000);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureStroke, StrokeAfterStrokeIsReportedAgain) {
    Sim s;
    s.PastGrace();
    for (int n = 0; n < 3; n++) {
        s.Zones(3, 0, 0, 150);
        s.Zones(0, 3, 0, 150);
        s.Zones(0, 0, 3, 200);
        s.Idle(1500);
    }
    EXPECT_EQ(s.strokes, 3);
}

// ---- 5. resting hand ---------------------------------------------------

TEST(TouchGestureHold, ThreeSecondsOnOneZoneGivesNothing) {
    for (int zone = 0; zone < 3; zone++) {
        Sim s;
        s.PastGrace();
        int c[3] = {0, 0, 0};
        c[zone] = 3;
        s.Zones(c[0], c[1], c[2], 3000);
        s.Idle(2000);
        EXPECT_EQ(s.taps, 0) << zone;
        EXPECT_EQ(s.strokes, 0) << zone;
    }
}

TEST(TouchGestureHold, HandOnAllZonesForFiveSecondsGivesNothing) {
    Sim s;
    s.PastGrace();
    s.Zones(2, 3, 2, 5000);
    s.Idle(2000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureHold, TapUpperBoundIsHalfASecond) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 3, 0, 450);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
    s.Zones(0, 3, 0, 700);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

// ---- 7. channel masking ------------------------------------------------

TEST(TouchGestureMask, StuckChannelIsReusedAfterOneSecondOfZeros) {
    Sim s;
    s.PastGrace();
    s.Raw(0x30, 16000);  // CH3 stuck, recal requested
    EXPECT_EQ(s.recal_requests, 1);
    EXPECT_EQ(s.tracker.masked_channels(), 0x04);
    s.Idle(500);
    EXPECT_EQ(s.tracker.masked_channels(), 0x04);
    s.Idle(600);
    EXPECT_EQ(s.tracker.masked_channels(), 0);
    s.Zones(0, 0, 3, 100);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

TEST(TouchGestureMask, OtherZonesStillTapWhileOneChannelIsStuck) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 0, 3, 16000);  // CH3 stuck for 16 s (masked after 15 s)
    s.Zones(3, 0, 3, 100);    // tap CH1 while CH3 stays stuck
    s.Zones(0, 0, 3, 1500);
    EXPECT_EQ(s.taps, 1);
    EXPECT_EQ(s.strokes, 0);
    EXPECT_EQ(s.tracker.masked_channels(), 0x04);
}

TEST(TouchGestureMask, PressInProgressWhenChannelIsMaskedIsNotReported) {
    Sim s;
    s.PastGrace();
    s.Zones(3, 0, 0, 100);
    s.Zones(3, 0, 3, 100);
    s.Zones(3, 0, 3, 15500);  // CH3 hits the 15 s limit during this press
    s.Idle(1000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureMask, RecalibrationsAreCounted) {
    Sim s;
    s.PastGrace();
    s.Raw(0x04, 31000);  // CH2 stuck, re-requested every 15 s
    EXPECT_EQ(s.tracker.recalibrations(), 2u);
}

}  // namespace
