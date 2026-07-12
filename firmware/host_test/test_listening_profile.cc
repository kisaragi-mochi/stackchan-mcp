#include "listening_profile.h"

#include <gtest/gtest.h>

TEST(ListeningProfileTest, MissingProfileFallsBackToVoice) {
    auto result = ParseListenProfileField(false, false, nullptr);

    EXPECT_EQ(result.profile, kListeningProfileVoice);
    EXPECT_EQ(result.warning, kListenProfileParseWarningNone);
}

TEST(ListeningProfileTest, UnknownStringFallsBackToVoiceWithWarning) {
    auto result = ParseListenProfileField(true, true, "beat");

    EXPECT_EQ(result.profile, kListeningProfileVoice);
    EXPECT_EQ(result.warning, kListenProfileParseWarningUnknown);
}

TEST(ListeningProfileTest, NonStringFallsBackToVoiceWithWarning) {
    auto result = ParseListenProfileField(true, false, nullptr);

    EXPECT_EQ(result.profile, kListeningProfileVoice);
    EXPECT_EQ(result.warning, kListenProfileParseWarningNonString);
}

TEST(ListeningProfileTest, RawStopRestoresVoiceProfile) {
    auto raw = ParseListenProfileField(true, true, "raw");

    ASSERT_EQ(raw.profile, kListeningProfileRaw);
    EXPECT_EQ(ListeningProfileAfterStop(raw.profile), kListeningProfileVoice);
}
