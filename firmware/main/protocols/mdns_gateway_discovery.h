#ifndef _MDNS_GATEWAY_DISCOVERY_H_
#define _MDNS_GATEWAY_DISCOVERY_H_

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

struct MdnsGatewayCandidate {
    std::string url;
    std::string instance_name;
    std::string hostname;
    std::string address;
    uint16_t port = 0;
    std::string path = "/";
    int result_count = 0;
};

// Returns one candidate per usable IPv4 address for all supported gateway
// services discovered in one mDNS browse. std::nullopt means no supported
// gateway was discovered.
std::optional<std::vector<MdnsGatewayCandidate>> DiscoverStackchanGateway(uint32_t timeout_ms);

#endif  // _MDNS_GATEWAY_DISCOVERY_H_
