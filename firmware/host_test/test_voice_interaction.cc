#include "voice_interaction.h"

#include <gtest/gtest.h>

TEST(VoiceInteractionTest, XiaozhiAutoTurnReturnsToListening) {
    EXPECT_EQ(
        DecideTtsStopTransition(VoiceInteractionMode::kXiaozhiConversational,
                                false),
        TtsStopTransition::kListening);
}

TEST(VoiceInteractionTest, XiaozhiManualPttTurnReturnsToIdle) {
    EXPECT_EQ(
        DecideTtsStopTransition(VoiceInteractionMode::kXiaozhiConversational,
                                true),
        TtsStopTransition::kIdle);
}

TEST(VoiceInteractionTest, McpSingleShotAlwaysReturnsToIdle) {
    EXPECT_EQ(
        DecideTtsStopTransition(VoiceInteractionMode::kMcpSingleShot, false),
        TtsStopTransition::kIdle);
    EXPECT_EQ(
        DecideTtsStopTransition(VoiceInteractionMode::kMcpSingleShot, true),
        TtsStopTransition::kIdle);
}

TEST(VoiceInteractionTest, TouchPttStartsAndStopsManualTurns) {
    EXPECT_EQ(DecideTouchPttAction(true, TouchPttState::kOther),
              TouchPttAction::kStartListening);
    EXPECT_EQ(DecideTouchPttAction(true, TouchPttState::kListening),
              TouchPttAction::kStopListening);
    EXPECT_EQ(DecideTouchPttAction(true, TouchPttState::kAudioTesting),
              TouchPttAction::kToggleAudioTesting);
}

TEST(VoiceInteractionTest, DisabledTouchPttIsInert) {
    EXPECT_EQ(DecideTouchPttAction(false, TouchPttState::kOther),
              TouchPttAction::kNone);
    EXPECT_EQ(DecideTouchPttAction(false, TouchPttState::kListening),
              TouchPttAction::kNone);
}
