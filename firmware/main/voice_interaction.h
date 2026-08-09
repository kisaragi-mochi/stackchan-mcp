#ifndef VOICE_INTERACTION_H
#define VOICE_INTERACTION_H

enum class VoiceInteractionMode {
    kXiaozhiConversational,
    kMcpSingleShot,
};

enum class TtsStopTransition {
    kIdle,
    kListening,
};

enum class TouchPttState {
    kAudioTesting,
    kListening,
    kOther,
};

enum class TouchPttAction {
    kNone,
    kToggleAudioTesting,
    kStartListening,
    kStopListening,
};

TtsStopTransition DecideTtsStopTransition(VoiceInteractionMode mode,
                                          bool manual_stop_session);
TouchPttAction DecideTouchPttAction(bool enabled, TouchPttState state);
const char* VoiceInteractionModeName(VoiceInteractionMode mode);

#endif  // VOICE_INTERACTION_H
