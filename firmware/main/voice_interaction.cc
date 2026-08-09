#include "voice_interaction.h"

TtsStopTransition DecideTtsStopTransition(VoiceInteractionMode mode,
                                          bool manual_stop_session) {
    if (mode == VoiceInteractionMode::kXiaozhiConversational &&
        !manual_stop_session) {
        return TtsStopTransition::kListening;
    }
    return TtsStopTransition::kIdle;
}

TouchPttAction DecideTouchPttAction(bool enabled, TouchPttState state) {
    if (!enabled) {
        return TouchPttAction::kNone;
    }
    if (state == TouchPttState::kAudioTesting) {
        return TouchPttAction::kToggleAudioTesting;
    }
    if (state == TouchPttState::kListening) {
        return TouchPttAction::kStopListening;
    }
    return TouchPttAction::kStartListening;
}

const char* VoiceInteractionModeName(VoiceInteractionMode mode) {
    return mode == VoiceInteractionMode::kXiaozhiConversational
        ? "XIAOZHI_CONVERSATIONAL"
        : "MCP_SINGLE_SHOT";
}
