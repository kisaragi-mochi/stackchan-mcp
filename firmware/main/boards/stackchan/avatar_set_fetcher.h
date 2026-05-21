#pragma once

#include <string>
#include <functional>
#include <cstdint>
#include <cstddef>

#include "avatar_set.h"

// AvatarSetFetcher — HTTP fetch + checksum verify + AvatarSet adoption.
//
// Invoked when the gateway sends a WS `avatar_set_fetch` message containing
// a URL, one-time bearer token, mode, expected size, and SHA256 checksum.
// The fetcher performs an authenticated HTTP GET against the gateway's
// staging endpoint (see docs/intent/stackchan_avatar_pipeline.md §C-2),
// verifies the SHA256, and hands the raw RGB565 payload to AvatarSet via
// AdoptOwnedBuffer() with ownership transfer (= no internal memcpy in the
// target set; PSRAM peak during swap is held to old + new only).
//
// On any failure the previously loaded set (if any) is left intact —
// AvatarSet only commits on AdoptOwnedBuffer success, and the fetcher's
// PSRAM buffer is freed in the failure path here.

class AvatarSetFetcher {
public:
    // Completion callback signature: (ok, actual_checksum, error_code).
    // - ok=true: actual_checksum equals the expected checksum and Load
    //   succeeded. error_code is empty.
    // - ok=false: error_code is one of:
    //     "http_open_failed", "content_length_mismatch",
    //     "psram_oom", "read_failed", "checksum_mismatch", "load_failed".
    //   actual_checksum is empty if the failure occurred before reading
    //   completed.
    using CompletionCallback = std::function<void(
        bool ok,
        const std::string& actual_checksum,
        const std::string& error_code)>;

    // Synchronously fetch + verify + load. Blocks the calling task until
    // completion. Intended to be invoked from a worker task (= not from
    // the WS receive callback directly).
    static void Fetch(
        AvatarSet& target_set,
        const std::string& url,
        const std::string& bearer_token,
        AvatarSet::Mode mode,
        size_t expected_size,
        const std::string& expected_sha256,  // "sha256:<hex>" form, may be empty to skip
        CompletionCallback on_complete);
};
