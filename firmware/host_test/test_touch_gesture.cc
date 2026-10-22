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

TEST(TouchGestureTap, LowLevelPressIsNeverATap) {
    for (uint32_t ms : {300u, 400u, 500u}) {
        Sim s;
        s.PastGrace();
        s.Raw(0x10, ms);  // CH3 = L
        s.Idle(1000);
        EXPECT_EQ(s.taps, 0) << "ms=" << ms;
    }
}

TEST(TouchGestureTap, ShortLowLevelBlipsAreDropped) {
    // The idle false positives seen on the device: one channel at L (level 1)
    // for 100..150 ms (CH1=0x01, CH2=0x04, CH3=0x10).
    const uint8_t raws[] = {0x01, 0x04, 0x10};
    for (uint8_t raw : raws) {
        for (uint32_t ms : {50u, 100u, 150u, 250u}) {
            Sim s;
            s.PastGrace();
            s.Raw(raw, ms);
            s.Idle(1000);
            EXPECT_EQ(s.taps, 0) << "raw=" << int(raw) << " ms=" << ms;
            EXPECT_EQ(s.strokes, 0) << "raw=" << int(raw) << " ms=" << ms;
        }
    }
}

TEST(TouchGestureTap, PressMustReachHighLevelToTap) {
    Sim s;
    s.PastGrace();
    s.Raw(0x01, 50);  // L, then M within the press: not a tap
    s.Raw(0x02, 50);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 0);
    Sim t;
    t.PastGrace();
    t.Raw(0x04, 50);  // L, then H for one tick (the light touches of 10/22)
    t.Raw(0x0C, 100);
    t.Idle(1000);
    EXPECT_EQ(t.taps, 1);
}

TEST(TouchGestureTap, LowLevelPressOver500msIsStillNotATap) {
    Sim s;
    s.PastGrace();
    s.Raw(0x10, 700);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 0);
}

// ---- settle window (charging / backlight switch) ---------------------------

TEST(TouchGestureSettle, PressesInsideTheWindowAreIgnoredAndReferenceIsResetAfterwards) {
    Sim s;
    s.PastGrace();
    s.tracker.NotifySettle(s.now);
    s.Zones(0, 3, 0, 300);  // would be a TAP
    s.Idle(600);            // still inside 1 s
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.recal_requests, 0);
    s.Idle(300);
    EXPECT_EQ(s.recal_requests, 1);
    EXPECT_EQ(s.recal_union, 0x07);
    EXPECT_EQ(s.tracker.settle_resets(), 1u);
    s.Idle(1500);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureSettle, TouchWorksAgainAfterTheWindowAndMaskClears) {
    Sim s;
    s.PastGrace();
    s.tracker.NotifySettle(s.now);
    s.Idle(3000);
    s.Zones(0, 3, 0, 200);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

TEST(TouchGestureSettle, PressAlreadyOnWhenSwitchHappensKeepsBeingTracked) {
    Sim s;
    s.PastGrace();
    s.Zones(0, 3, 0, 100);
    s.tracker.NotifySettle(s.now);
    s.Zones(0, 3, 0, 100);
    s.Idle(3000);
    EXPECT_EQ(s.taps, 1);
}

TEST(TouchGestureSettle, StrokeStartingInsideTheWindowIsStillReported) {
    // Device log 1573.8 s: CH3 -> CH2 -> CH1 sweep 0.4 s after a charging flip.
    Sim s;
    s.PastGrace();
    s.tracker.NotifySettle(s.now);
    s.Idle(200);
    s.Zones(0, 0, 3, 150);
    s.Zones(0, 3, 3, 100);
    s.Zones(0, 3, 0, 100);
    s.Zones(3, 3, 0, 100);
    s.Zones(3, 0, 0, 150);
    s.Idle(1500);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureSettle, StrokesAreReportedWhileChargingFlapsEveryTwoSeconds) {
    // Device log 1517..1574 s: charging flipped every 1..3 s while the user
    // was stroking. Every stroke must come through whatever its phase.
    for (uint32_t phase = 0; phase < 1400; phase += 100) {
        Sim s;
        s.PastGrace();
        uint32_t next_flip = s.now;
        int expected = 0;
        for (int n = 0; n < 6; n++) {
            s.Idle(phase + 100 * n);
            while (s.now >= next_flip) {
                s.tracker.NotifySettle(next_flip);
                next_flip += 1200;
            }
            s.Zones(0, 0, 3, 100);
            s.Zones(0, 3, 3, 100);
            s.Zones(3, 3, 0, 100);
            s.Zones(3, 0, 0, 100);
            expected++;
            s.Idle(1500);
            while (s.now >= next_flip) {
                s.tracker.NotifySettle(next_flip);
                next_flip += 1200;
            }
        }
        EXPECT_EQ(s.strokes, expected) << "phase=" << phase;
    }
}

TEST(TouchGestureSettle, IdleBlipsFromTheDeviceLogAfterChargingFlipsGiveNothing) {
    // 285.6 s: CH3 H for 450 ms right after a flip, then M->L for 300 ms.
    // 465.9 s: CH2 L lingering 1 s. 898.9 s: CH3 L 150 ms.
    struct Blip { uint8_t first, second; uint32_t first_ms, second_ms, after_flip_ms; };
    const Blip blips[] = {
        {0x30, 0x00, 450, 0, 100}, {0x20, 0x10, 150, 150, 50}, {0x04, 0x04, 500, 500, 0},
        {0x10, 0x10, 100, 50, 150},
    };
    for (const Blip& b : blips) {
        Sim s;
        s.PastGrace();
        s.tracker.NotifySettle(s.now);
        s.Idle(b.after_flip_ms);
        s.Raw(b.first, b.first_ms);
        if (b.second_ms) s.Raw(b.second, b.second_ms);
        s.Idle(3000);
        EXPECT_EQ(s.taps, 0) << int(b.first);
        EXPECT_EQ(s.strokes, 0) << int(b.first);
    }
}

TEST(TouchGestureSettle, ResetWaitsForAStrokeThatOutlastsTheWindow) {
    Sim s;
    s.PastGrace();
    s.Zones(3, 0, 0, 200);
    s.tracker.NotifySettle(s.now);
    s.Zones(0, 3, 0, 1500);
    EXPECT_EQ(s.recal_requests, 0);
    s.Idle(500);
    EXPECT_EQ(s.recal_requests, 1);
    EXPECT_EQ(s.strokes, 1);
}

TEST(TouchGestureSettle, TouchRightAfterTheResetIsNotMasked) {
    Sim s;
    s.PastGrace();
    s.tracker.NotifySettle(s.now);
    s.Idle(1100);
    EXPECT_EQ(s.recal_requests, 1);
    EXPECT_EQ(s.tracker.masked_channels(), 0);
    s.Zones(0, 3, 0, 200);
    s.Idle(1000);
    EXPECT_EQ(s.taps, 1);
}

TEST(TouchGestureSettle, BlipThatLingersUntilTheResetIsDroppedAndUnmaskedQuickly) {
    // Device log 313 s / 465 s: a single L that stays until the baseline reset.
    Sim s;
    s.PastGrace();
    s.tracker.NotifySettle(s.now);
    s.Raw(0x10, 1400);
    EXPECT_EQ(s.recal_requests, 1);
    EXPECT_EQ(s.tracker.masked_channels(), 0x04);
    s.Idle(200);
    EXPECT_EQ(s.tracker.masked_channels(), 0x04);
    s.Idle(200);
    EXPECT_EQ(s.tracker.masked_channels(), 0);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureSettle, BlipAtChargingFlipFromTheDeviceLogGivesNothing) {
    // 1027.9 s: CH3 M for 100 ms, then L for 1 s while charging flipped 0->1.
    Sim s;
    s.PastGrace();
    s.Raw(0x20, 100);
    s.tracker.NotifySettle(s.now);  // flip noticed one tick late
    s.Raw(0x10, 1000);
    s.Idle(3000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
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

// ---- 6. wide, fast strokes from the 10/21 device logs --------------------

// Each raw byte is held for one 50 ms tick, then the pad is released.
void ReplayPress(Sim& s, const std::vector<uint8_t>& raws) {
    s.PastGrace();
    for (uint8_t r : raws) s.Raw(r, kTick);
    s.Idle(1000);
}

TEST(TouchGestureWideStroke, StartChannelNeverLeavesStillStrokes) {
    // 705.4 s: peak H, 650 ms, CH1 stays on until the release (was "ignored").
    Sim s;
    std::vector<uint8_t> raws = {0x03, 0x0F, 0x0F, 0x0F, 0x1F, 0x1F, 0x1F, 0x3D, 0x3E, 0x3E};
    ReplayPress(s, raws);
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureWideStroke, FastFrontToBackWithOneTickAtTheEndStrokes) {
    // 686.1 s: 0x30 -> 0x3C -> 0x3D -> 0x07 (was a TAP).
    Sim s;
    ReplayPress(s, {0x30, 0x3C, 0x3D, 0x07});
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureWideStroke, FastBackToFrontStrokes) {
    // 697.0 s: 0x0F -> 0x0E -> 0x30.
    Sim s;
    ReplayPress(s, {0x0F, 0x0E, 0x30});
    EXPECT_EQ(s.strokes, 1);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureWideStroke, FirstTwoMissedStrokesOfTheFirstRound) {
    // c log 1572 s / 1575 s: 0x30 0x3C 0x2F 0x0F 0x03 0x01 and 0x30 0x3C 0x0F 0x03.
    Sim s;
    ReplayPress(s, {0x30, 0x3C, 0x2F, 0x0F, 0x03, 0x01});
    ReplayPress(s, {0x30, 0x3C, 0x0F, 0x03});
    EXPECT_EQ(s.strokes, 2);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureWideStroke, TapsFromTheDeviceLogStayTaps) {
    Sim s;
    ReplayPress(s, {0x38, 0x30});          // 665.6 s, back
    ReplayPress(s, {0x20, 0x3C});          // 674.0 s, back with spill
    ReplayPress(s, {0x0F, 0x0F, 0x0B});    // front
    EXPECT_EQ(s.taps, 3);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureWideStroke, LowLevelBlipsAcrossZonesAreNotAStroke) {
    // All-L patterns (the 0x15 / 0x10 blips) never reach level M.
    Sim s;
    ReplayPress(s, {0x15, 0x15, 0x14, 0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x10});
    EXPECT_EQ(s.strokes, 0);
}

// ---- 7. bursts on an untouched head (10/21 22:4x-23:0x, nobody near) -----

TEST(TouchGestureNoiseBurst, OneTickMhhSpikeIsNotATap) {
    // 1631.6 s, screen on: 0x3E for a single 50 ms tick.
    Sim s;
    ReplayPress(s, {0x3E});
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureNoiseBurst, MediumBurstsAreNotTaps) {
    Sim s;
    ReplayPress(s, {0x29, 0x29});                                  // 967.1 s
    ReplayPress(s, {0x26, 0x26, 0x26, 0x26, 0x26, 0x2A, 0x2A, 0x0A});  // 1355.6 s
    ReplayPress(s, {0x29, 0x29, 0x29, 0x29});                      // 1943.4 s
    ReplayPress(s, {0x05, 0x15, 0x10});                            // weak L, 1942.7 s
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureNoiseBurst, FirmTouchWakesAScreenThatIsOff) {
    // c log 1479.7 s and h log 665.6 s: the real wake-up taps reached H.
    Sim s;
    ReplayPress(s, {0x30, 0x30, 0x30});
    ReplayPress(s, {0x38, 0x30, 0x30});
    EXPECT_EQ(s.taps, 2);
}

// 10/23 00:47-00:51 (CI build, screen on, nobody near): 17 TAPs, all of
// them L/M only. Each press below is one of them, sample by sample.
TEST(TouchGestureNoiseBurst, IdleBurstsWithTheScreenOnAreNotTaps) {
    Sim s;
    ReplayPress(s, std::vector<uint8_t>(8, 0x15));                // LLL 400 ms
    ReplayPress(s, {0x25, 0x25});                                 // LLM 100 ms
    ReplayPress(s, {0x19, 0x19, 0x19, 0x29, 0x29, 0x19});         // LML 300 ms
    ReplayPress(s, std::vector<uint8_t>(8, 0x02));                // M000 400 ms
    ReplayPress(s, {0x12, 0x12, 0x12, 0x12, 0x12});               // M0L0 250 ms
    ReplayPress(s, {0x28, 0x28, 0x28, 0x28, 0x28, 0x18, 0x18, 0x18});  // 0MM0 400 ms
    ReplayPress(s, {0x18, 0x18});                                 // 0ML0 100 ms
    ReplayPress(s, {0x15, 0x25, 0x26, 0x26, 0x16});               // LLL->LMM 250 ms
    ReplayPress(s, {0x20, 0x20});                                 // 00M0 100 ms
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureNoiseBurst, MediumBurstThatShiftsTheCentreIsNotAStroke) {
    // 10/23 02:37:36-40 (CI build, nobody near): CH1+CH3 at M, then CH3 only.
    // The level-weighted centre moves by one zone, but nothing reached H.
    Sim s;
    ReplayPress(s, {0x22, 0x22, 0x22, 0x22, 0x22, 0x22, 0x20});
    EXPECT_EQ(s.strokes, 0);
    EXPECT_EQ(s.moved, 0);
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureNoiseBurst, AllZoneBurstWithOneHighIsNotATap) {
    // 10/23 03:10:40 (fixed build, screen off, nobody near) and 10/21 j log
    // 255.8 s (screen on): 0x3A = CH1 M, CH2 M, CH3 H for 150 ms.
    Sim s;
    ReplayPress(s, {0x3A, 0x3A, 0x3A});
    EXPECT_EQ(s.taps, 0);
}

TEST(TouchGestureNoiseBurst, RealTapsStartingOnAllZonesStillTap) {
    // c log 1487.8 s / 1535.2 s and k log 359.8 s / 362.6 s.
    Sim s;
    ReplayPress(s, {0x1F, 0x0F});
    ReplayPress(s, {0x2F, 0x0F});
    ReplayPress(s, {0x3D, 0x0C});
    ReplayPress(s, {0x3D, 0x3D});
    EXPECT_EQ(s.taps, 4);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureNoiseBurst, MediumPulseEveryFourSecondsGivesNothing) {
    // 00:49:23-00:49:47: CH1 at M, on for 0.4 s or ~3.2 s, every ~4 s.
    Sim s;
    s.PastGrace();
    for (int i = 0; i < 10; i++) {
        s.Raw(0x02, (i % 3 == 0) ? 3200 : 400);
        s.Idle(4000 - ((i % 3 == 0) ? 3200 : 400));
    }
    s.Idle(1000);
    EXPECT_EQ(s.taps, 0);
    EXPECT_EQ(s.strokes, 0);
}

TEST(TouchGestureNoiseBurst, FirmStrokeIsStillAStroke) {
    Sim s;
    ReplayPress(s, {0x30, 0x3C, 0x0F, 0x03});
    EXPECT_EQ(s.strokes, 1);
}

}  // namespace
