#include "mdns_gateway_discovery.h"

#include <esp_log.h>

#include <cstdio>
#include <cstring>
#include <utility>
#include <vector>

#if CONFIG_STACKCHAN_MDNS_DISCOVERY
#include <esp_err.h>
#include <esp_netif_ip_addr.h>
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

std::string SafeString(const char* value) {
    return value == nullptr ? std::string() : std::string(value);
}

int CountResults(const mdns_result_t* results) {
    int count = 0;
    for (const mdns_result_t* result = results; result != nullptr; result = result->next) {
        ++count;
    }
    return count;
}

std::optional<std::string> TxtValue(const mdns_result_t* result, const char* key) {
    if (result == nullptr || key == nullptr) {
        return std::nullopt;
    }
    for (size_t i = 0; i < result->txt_count; ++i) {
        if (result->txt[i].key == nullptr || strcmp(result->txt[i].key, key) != 0) {
            continue;
        }
        if (result->txt[i].value == nullptr) {
            return std::string();
        }
        if (result->txt_value_len != nullptr) {
            return std::string(result->txt[i].value, result->txt_value_len[i]);
        }
        return std::string(result->txt[i].value);
    }
    return std::nullopt;
}

std::string NormalizePath(const std::optional<std::string>& maybe_path) {
    if (!maybe_path.has_value() || maybe_path->empty()) {
        return "/";
    }
    if ((*maybe_path)[0] == '/') {
        return *maybe_path;
    }
    return "/" + *maybe_path;
}

bool IsUsableIpv4String(const std::string& address) {
    if (address.empty() || address == "0.0.0.0") {
        return false;
    }
    if (address.rfind("127.", 0) == 0) {
        return false;
    }
    int first_octet = 0;
    if (sscanf(address.c_str(), "%d", &first_octet) != 1) {
        return false;
    }
    return first_octet < 224;
}

std::vector<std::string> UsableIpv4Addresses(const mdns_result_t* result) {
    std::vector<std::string> addresses;
    for (mdns_ip_addr_t* address = result == nullptr ? nullptr : result->addr;
         address != nullptr;
         address = address->next) {
        if (address->addr.type != ESP_IPADDR_TYPE_V4) {
            continue;
        }
        char buffer[16] = {0};
        snprintf(buffer, sizeof(buffer), IPSTR, IP2STR(&address->addr.u_addr.ip4));
        std::string ipv4(buffer);
        if (!IsUsableIpv4String(ipv4)) {
            continue;
        }
        addresses.push_back(ipv4);
    }
    return addresses;
}

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

std::string JoinAddresses(const std::vector<std::string>& addresses) {
    if (addresses.empty()) {
        return std::string();
    }
    std::string joined = addresses.front();
    for (size_t i = 1; i < addresses.size(); ++i) {
        joined += ",";
        joined += addresses[i];
    }
    return joined;
}

std::string BuildWebSocketUrl(const std::string& address, uint16_t port, const std::string& path) {
    return "ws://" + address + ":" + std::to_string(port) + path;
}

struct ExtractedGatewayCandidates {
    int accepted_instances = 0;
    std::vector<MdnsGatewayCandidate> candidates;
};

ExtractedGatewayCandidates ExtractGatewayCandidatesFromMdnsResults(const mdns_result_t* results,
                                                                   int result_count) {
    ExtractedGatewayCandidates extracted;
    for (const mdns_result_t* result = results; result != nullptr; result = result->next) {
        std::string instance_name = SafeString(result->instance_name);
        std::string hostname = SafeString(result->hostname);

        auto version = TxtValue(result, "version");
        if (version.has_value() && *version != "1") {
            ESP_LOGI(TAG,
                     "Skipping mDNS gateway instance=\"%s\" host=\"%s\": unsupported TXT version=\"%s\"",
                     instance_name.c_str(), hostname.c_str(), version->c_str());
            continue;
        }

        if (result->port == 0) {
            ESP_LOGW(TAG, "Skipping mDNS gateway instance=\"%s\" host=\"%s\": zero port",
                     instance_name.c_str(), hostname.c_str());
            continue;
        }

        auto addresses = UsableIpv4Addresses(result);
        if (addresses.empty()) {
            ESP_LOGI(TAG, "Skipping mDNS gateway instance=\"%s\" host=\"%s\": no usable IPv4 address",
                     instance_name.c_str(), hostname.c_str());
            continue;
        }

        std::string path = NormalizePath(TxtValue(result, "path"));
        ESP_LOGI(TAG,
                 "Accepting mDNS gateway instance=\"%s\" host=\"%s\" port=%u path=\"%s\" addresses=%s",
                 instance_name.c_str(), hostname.c_str(),
                 static_cast<unsigned>(result->port), path.c_str(),
                 JoinAddresses(addresses).c_str());
        ++extracted.accepted_instances;
        for (const auto& address : addresses) {
            MdnsGatewayCandidate candidate;
            candidate.url = BuildWebSocketUrl(address, result->port, path);
            candidate.instance_name = instance_name;
            candidate.hostname = hostname;
            candidate.address = address;
            candidate.port = result->port;
            candidate.path = path;
            candidate.result_count = result_count;
            extracted.candidates.push_back(candidate);
        }
    }
    return extracted;
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

    int result_count = CountResults(results);
    // Keep the count next to the extracted candidates so summary logs stay
    // accurate without a mutable out-parameter.
    auto extracted = ExtractGatewayCandidatesFromMdnsResults(results, result_count);
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
