#include "avatar_set_fetcher.h"

#include <algorithm>
#include <cstdio>

#include <mbedtls/sha256.h>
#include <esp_heap_caps.h>
#include <esp_log.h>

#include "board.h"

#define TAG "AvatarSetFetcher"

namespace {

constexpr size_t kReadChunkBytes = 4096;

std::string Sha256Hex(const uint8_t* data, size_t size) {
    uint8_t hash[32];
    mbedtls_sha256(data, size, hash, 0);
    char hex[65];
    for (int i = 0; i < 32; ++i) {
        std::snprintf(hex + i * 2, 3, "%02x", hash[i]);
    }
    hex[64] = '\0';
    return std::string("sha256:") + hex;
}

}  // namespace

void AvatarSetFetcher::Fetch(
    AvatarSet& target_set,
    const std::string& url,
    const std::string& bearer_token,
    AvatarSet::Mode mode,
    size_t expected_size,
    const std::string& expected_sha256,
    CompletionCallback on_complete) {
    auto& board = Board::GetInstance();
    auto network = board.GetNetwork();
    if (network == nullptr) {
        ESP_LOGE(TAG, "Fetch: network is null");
        on_complete(false, "", "http_open_failed");
        return;
    }

    auto http = network->CreateHttp(0);
    if (http == nullptr) {
        ESP_LOGE(TAG, "Fetch: CreateHttp returned null");
        on_complete(false, "", "http_open_failed");
        return;
    }

    http->SetHeader("Authorization", (std::string("Bearer ") + bearer_token).c_str());
    http->SetHeader("Accept", "application/octet-stream");

    if (!http->Open("GET", url)) {
        ESP_LOGE(TAG, "Fetch: Open failed for url=%s", url.c_str());
        on_complete(false, "", "http_open_failed");
        return;
    }

    const size_t content_length = http->GetBodyLength();
    if (content_length != expected_size) {
        ESP_LOGW(TAG, "Fetch: Content-Length mismatch (got=%zu, expected=%zu)",
                 content_length, expected_size);
        http->Close();
        on_complete(false, "", "content_length_mismatch");
        return;
    }

    // Allocate PSRAM staging buffer.
    //
    // Note: AvatarSet::Load currently copies its input into a fresh PSRAM
    // buffer, so during Load() the firmware temporarily holds 2× the set
    // size in PSRAM (this buffer + the AvatarSet's own buffer). For matrix
    // mode (~3.3 MB) this approaches the PSRAM ceiling. A follow-up
    // optimization is to switch AvatarSet::Load to ownership-transfer
    // semantics (e.g., std::unique_ptr<uint8_t[], PsramDeleter>), at which
    // point this buffer can be handed directly to AvatarSet without a copy.
    uint8_t* buffer = static_cast<uint8_t*>(
        heap_caps_malloc(expected_size, MALLOC_CAP_SPIRAM));
    if (buffer == nullptr) {
        ESP_LOGE(TAG, "Fetch: PSRAM staging allocation failed (size=%zu)", expected_size);
        http->Close();
        on_complete(false, "", "psram_oom");
        return;
    }

    size_t total_read = 0;
    while (total_read < expected_size) {
        const size_t to_read = std::min(kReadChunkBytes, expected_size - total_read);
        int n = http->Read(reinterpret_cast<char*>(buffer + total_read),
                           static_cast<int>(to_read));
        if (n <= 0) {
            ESP_LOGE(TAG, "Fetch: Read failed at offset %zu (n=%d)", total_read, n);
            heap_caps_free(buffer);
            http->Close();
            on_complete(false, "", "read_failed");
            return;
        }
        total_read += static_cast<size_t>(n);
    }
    http->Close();

    const std::string actual_sha256 = Sha256Hex(buffer, expected_size);

    if (!expected_sha256.empty() && actual_sha256 != expected_sha256) {
        ESP_LOGW(TAG, "Fetch: SHA256 mismatch (actual=%s expected=%s)",
                 actual_sha256.c_str(), expected_sha256.c_str());
        heap_caps_free(buffer);
        on_complete(false, actual_sha256, "checksum_mismatch");
        return;
    }

    const bool loaded = target_set.Load(mode, buffer, expected_size);
    heap_caps_free(buffer);  // AvatarSet::Load copies internally.

    if (!loaded) {
        on_complete(false, actual_sha256, "load_failed");
        return;
    }

    ESP_LOGI(TAG, "Fetch: avatar set loaded (mode=%d, bytes=%zu, sha256=%s)",
             static_cast<int>(mode), expected_size, actual_sha256.c_str());
    on_complete(true, actual_sha256, "");
}
