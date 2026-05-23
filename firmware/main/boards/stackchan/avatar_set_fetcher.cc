#include "avatar_set_fetcher.h"

#include <algorithm>
#include <cstdio>

#include <mbedtls/sha256.h>
#include <esp_heap_caps.h>
#include <esp_log.h>

#include "board.h"
#include "display/display.h"

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
        ESP_LOGW(TAG, "Fetch: Content-Length mismatch (got=%u, expected=%u)",
                 static_cast<unsigned int>(content_length),
                 static_cast<unsigned int>(expected_size));
        http->Close();
        on_complete(false, "", "content_length_mismatch");
        return;
    }

    // Allocate the PSRAM buffer that will become the AvatarSet's owned
    // image_buffer_ on success. We fill it via HTTP read, verify the SHA256,
    // then hand it off to AvatarSet::AdoptOwnedBuffer with ownership transfer
    // (= no internal memcpy). PSRAM peak during a swap is therefore
    // (old AvatarSet buffer + this buffer) and not 3× the set size.
    uint8_t* buffer = static_cast<uint8_t*>(
        heap_caps_malloc(expected_size, MALLOC_CAP_SPIRAM));
    if (buffer == nullptr) {
        ESP_LOGE(TAG, "Fetch: PSRAM staging allocation failed (size=%u)",
                 static_cast<unsigned int>(expected_size));
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
            ESP_LOGE(TAG, "Fetch: Read failed at offset %u (n=%d)",
                     static_cast<unsigned int>(total_read), n);
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

    // Hand ownership to AvatarSet. On success it owns `buffer` and will
    // free it on the next Unload() / destruction; on failure ownership
    // stays with us and we must free it ourselves.
    //
    // AdoptOwnedBuffer frees the previously adopted PSRAM buffer and
    // rewrites the lv_image_dsc_t descriptors backing the on-screen avatar.
    // OnAvatarSetFetch quiesced the autonomous LVGL writers (lipsync / mouth
    // sequence / blink timers) before spawning this task, but that only
    // blocks new set_src writes — the LVGL display task can still be reading
    // the current descriptor source. Hold the display lock across the swap so
    // a concurrent render can't race into a half-freed buffer or a
    // half-cleared descriptor.
    auto* display = board.GetDisplay();
    bool loaded;
    if (display != nullptr) {
        DisplayLockGuard lock(display);
        loaded = target_set.AdoptOwnedBuffer(mode, buffer, expected_size);
    } else {
        // Headless board (NoDisplay): no LVGL reader to race against.
        loaded = target_set.AdoptOwnedBuffer(mode, buffer, expected_size);
    }
    if (!loaded) {
        heap_caps_free(buffer);
        on_complete(false, actual_sha256, "load_failed");
        return;
    }

    // ESP-IDF defaults to newlib-nano printf which does NOT support %zu;
    // a stray %zu is silently skipped, shifting downstream arguments — in
    // particular the next %s reads a size_t value as a const char* and
    // dereferences it, blowing up with LoadProhibited @ <that size>.
    // Cast size_t to unsigned int and use %u to stay nano-printf-safe.
    ESP_LOGI(TAG, "Fetch: avatar set loaded (mode=%d, bytes=%u, sha256=%s)",
             static_cast<int>(mode),
             static_cast<unsigned int>(expected_size),
             actual_sha256.c_str());
    on_complete(true, actual_sha256, "");
}
