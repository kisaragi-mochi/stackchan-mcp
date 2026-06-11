#include "mdns_gateway_discovery.h"

#include <esp_log.h>

#include <utility>
#include <vector>

#if CONFIG_STACKCHAN_MDNS_DISCOVERY
#include "mdns_candidate_extract.h"

#include <esp_err.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mdns.h>
#endif

#define TAG "WS"

namespace {

constexpr char kServiceType[] = "_stackchan-mcp";
constexpr char kProtocol[] = "_tcp";
constexpr size_t kMaxResults = 8;

#if CONFIG_STACKCHAN_MDNS_DISCOVERY

constexpr int kQueryAttempts = 3;
constexpr uint32_t kQueryRetryGapMs = 200;

std::string JoinCandidateAddresses(const std::vector<MdnsGatewayCandidate>& candidates) {
    if (candidates.empty()) {
        return std::string();
    }
    std::string joined = candidates.front().address;
    for (size_t i = 1; i < candidates.size(); ++i) {
        joined += ",";
        joined += candidates[i].address;
    }
    return joined;
}

#endif  // CONFIG_STACKCHAN_MDNS_DISCOVERY

}  // namespace

std::optional<std::vector<MdnsGatewayCandidate>> DiscoverStackchanGateway(uint32_t timeout_ms) {
#if CONFIG_STACKCHAN_MDNS_DISCOVERY
    mdns_result_t* results = nullptr;
    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS discovery unavailable: mdns_init failed: %s", esp_err_to_name(err));
        return std::nullopt;
    }

    wifi_ps_type_t previous_ps_mode = WIFI_PS_MIN_MODEM;
    esp_err_t ps_get_err = esp_wifi_get_ps(&previous_ps_mode);
    if (ps_get_err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to read WiFi power-save mode before mDNS browse: %s",
                 esp_err_to_name(ps_get_err));
    }

    esp_err_t ps_set_err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (ps_set_err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to disable WiFi power-save during mDNS browse: %s",
                 esp_err_to_name(ps_set_err));
    }

    auto restore_wifi_power_save = [&]() {
        esp_err_t ps_restore_err = esp_wifi_set_ps(previous_ps_mode);
        if (ps_restore_err != ESP_OK) {
            ESP_LOGW(TAG, "Failed to restore WiFi power-save mode after mDNS browse: %s",
                     esp_err_to_name(ps_restore_err));
        }
    };

    for (int attempt = 0; attempt < kQueryAttempts; ++attempt) {
        if (attempt > 0) {
            vTaskDelay(pdMS_TO_TICKS(kQueryRetryGapMs));
        }

        err = mdns_query_ptr(kServiceType, kProtocol, timeout_ms, kMaxResults, &results);
        if (err == ESP_OK && results != nullptr) {
            break;
        }

        if (results != nullptr) {
            mdns_query_results_free(results);
            results = nullptr;
        }
        ESP_LOGI(TAG, "mDNS query attempt %d/%d returned no results",
                 attempt + 1, kQueryAttempts);
    }

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS gateway query failed after %d attempts: %s",
                 kQueryAttempts, esp_err_to_name(err));
        if (results != nullptr) {
            mdns_query_results_free(results);
        }
        restore_wifi_power_save();
        mdns_free();
        return std::nullopt;
    }

    int result_count = stackchan::mdns::CountResults(results);
    // Keep the count next to the extracted candidates so summary logs stay
    // accurate without a mutable out-parameter.
    auto extracted = stackchan::mdns::ExtractGatewayCandidatesFromMdnsResults(results, result_count);
    std::optional<std::vector<MdnsGatewayCandidate>> all_candidates;
    if (!extracted.candidates.empty()) {
        all_candidates = std::move(extracted.candidates);
    }

    if (all_candidates.has_value()) {
        std::string addresses = JoinCandidateAddresses(*all_candidates);
        // Cast size_t to unsigned int and use %u to stay nano-printf-safe
        // (newlib-nano in ESP-IDF does not handle %zu; the misaligned arg
        // would then read the size_t as a string pointer and crash). Same
        // pattern as firmware/main/boards/stackchan/avatar_set_fetcher.cc.
        ESP_LOGI(TAG,
                 "mDNS gateway browse complete: raw_results=%d accepted_instances=%d candidates=%u addresses=%s",
                 result_count,
                 extracted.accepted_instances,
                 static_cast<unsigned int>(all_candidates->size()),
                 addresses.c_str());
    } else if (result_count == 0) {
        ESP_LOGI(TAG, "No mDNS stackchan gateway services discovered");
    } else {
        ESP_LOGW(TAG, "mDNS gateway browse found %d result(s), but no supported gateway candidates",
                 result_count);
    }

    if (results != nullptr) {
        mdns_query_results_free(results);
    }
    restore_wifi_power_save();
    mdns_free();
    return all_candidates;
#else
    (void)timeout_ms;
    return std::nullopt;
#endif
}
