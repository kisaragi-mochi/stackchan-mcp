#include "mdns_gateway_discovery.h"

#include <esp_log.h>

#include <cstdio>
#include <cstring>
#include <vector>

#if CONFIG_STACKCHAN_MDNS_DISCOVERY
#include <esp_err.h>
#include <esp_netif_ip_addr.h>
#include <mdns.h>
#endif

#define TAG "WS"

namespace {

constexpr char kServiceType[] = "_stackchan-mcp";
constexpr char kProtocol[] = "_tcp";
constexpr size_t kMaxResults = 8;

#if CONFIG_STACKCHAN_MDNS_DISCOVERY

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

std::string BuildWebSocketUrl(const std::string& address, uint16_t port, const std::string& path) {
    return "ws://" + address + ":" + std::to_string(port) + path;
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

    err = mdns_query_ptr(kServiceType, kProtocol, timeout_ms, kMaxResults, &results);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS gateway query failed: %s", esp_err_to_name(err));
        if (results != nullptr) {
            mdns_query_results_free(results);
        }
        mdns_free();
        return std::nullopt;
    }

    int result_count = CountResults(results);
    std::optional<std::vector<MdnsGatewayCandidate>> selected;

    for (mdns_result_t* result = results; result != nullptr; result = result->next) {
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
        std::vector<MdnsGatewayCandidate> candidates;
        candidates.reserve(addresses.size());
        for (const auto& address : addresses) {
            MdnsGatewayCandidate candidate;
            candidate.url = BuildWebSocketUrl(address, result->port, path);
            candidate.instance_name = instance_name;
            candidate.hostname = hostname;
            candidate.address = address;
            candidate.port = result->port;
            candidate.path = path;
            candidate.result_count = result_count;
            candidates.push_back(candidate);
        }
        selected = candidates;
        break;
    }

    if (selected.has_value()) {
        const auto& first = selected->front();
        std::string addresses = JoinCandidateAddresses(*selected);
        ESP_LOGI(TAG,
                 "mDNS discovered %d stackchan gateway service(s); selected instance=\"%s\" host=\"%s\" addresses=%s port=%u path=\"%s\"",
                 first.result_count,
                 first.instance_name.c_str(),
                 first.hostname.c_str(),
                 addresses.c_str(),
                 first.port,
                 first.path.c_str());
    } else if (result_count == 0) {
        ESP_LOGI(TAG, "No mDNS stackchan gateway services discovered");
    } else {
        ESP_LOGI(TAG, "No supported mDNS stackchan gateway service found among %d result(s)", result_count);
    }

    if (results != nullptr) {
        mdns_query_results_free(results);
    }
    mdns_free();
    return selected;
#else
    (void)timeout_ms;
    return std::nullopt;
#endif
}
