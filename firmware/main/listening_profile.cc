#include "listening_profile.h"

#include <cstring>

ListenProfileParseResult ParseListenProfileField(bool profile_present,
                                                 bool profile_is_string,
                                                 const char* profile_value) {
    if (!profile_present) {
        return {};
    }
    if (!profile_is_string) {
        return {kListeningProfileVoice, kListenProfileParseWarningNonString};
    }
    if (profile_value != nullptr && strcmp(profile_value, "raw") == 0) {
        return {kListeningProfileRaw, kListenProfileParseWarningNone};
    }
    if (profile_value != nullptr && strcmp(profile_value, "voice") == 0) {
        return {kListeningProfileVoice, kListenProfileParseWarningNone};
    }
    return {kListeningProfileVoice, kListenProfileParseWarningUnknown};
}

ListeningProfile ListeningProfileAfterStop(ListeningProfile profile) {
    (void)profile;
    return kListeningProfileVoice;
}
