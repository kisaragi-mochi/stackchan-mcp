#include "boards/stackchan/power_save_gate.h"

#include <gtest/gtest.h>

namespace {

using stackchan::power::PowerSaveGate;

// The board starts PowerSaveTimer enabled before the first battery poll.
constexpr bool kTimerEnabledAtBoot = true;

TEST(PowerSaveGateTest, BootOnExternalPowerDisablesTimer) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = true;
    ASSERT_TRUE(gate.Update(/*discharging=*/false,
                            /*external_power_auto_off=*/false, &enable));
    EXPECT_FALSE(enable);
    EXPECT_FALSE(gate.Update(false, false, &enable));
}

TEST(PowerSaveGateTest, BootOnBatteryKeepsTimer) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = false;
    EXPECT_FALSE(gate.Update(/*discharging=*/true, false, &enable));
    EXPECT_TRUE(gate.applied());
}

TEST(PowerSaveGateTest, FollowsPowerSourceTransitions) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = false;
    ASSERT_TRUE(gate.Update(false, false, &enable));  // USB at boot
    EXPECT_FALSE(enable);
    ASSERT_TRUE(gate.Update(true, false, &enable));  // unplugged
    EXPECT_TRUE(enable);
    ASSERT_TRUE(gate.Update(false, false, &enable));  // plugged back in
    EXPECT_FALSE(enable);
}

TEST(PowerSaveGateTest, OptInKeepsTimerOnExternalPower) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = false;
    EXPECT_FALSE(gate.Update(false, /*external_power_auto_off=*/true, &enable));
    EXPECT_TRUE(gate.applied());
    // Turning the opt-in off while on USB stops the timer.
    ASSERT_TRUE(gate.Update(false, false, &enable));
    EXPECT_FALSE(enable);
    // Turning it back on restarts it.
    ASSERT_TRUE(gate.Update(false, true, &enable));
    EXPECT_TRUE(enable);
}

}  // namespace
