#include "mdns_candidate_extract.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <string>
#include <vector>

namespace {

using stackchan::mdns::CountResults;
using stackchan::mdns::ExtractGatewayCandidatesFromMdnsResults;

esp_ip_addr_t Ipv4(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
    esp_ip_addr_t ip{};
    ip.type = ESP_IPADDR_TYPE_V4;
    ip.u_addr.ip4.addr = (static_cast<uint32_t>(a) << 24) |
                         (static_cast<uint32_t>(b) << 16) |
                         (static_cast<uint32_t>(c) << 8) |
                         static_cast<uint32_t>(d);
    return ip;
}

esp_ip_addr_t Ipv6() {
    esp_ip_addr_t ip{};
    ip.type = ESP_IPADDR_TYPE_V6;
    return ip;
}

mdns_ip_addr_t MdnsAddress(const esp_ip_addr_t& ip, mdns_ip_addr_t* next = nullptr) {
    mdns_ip_addr_t address{};
    address.addr = ip;
    address.next = next;
    return address;
}

mdns_result_t MdnsResult(const char* instance_name,
                         const char* hostname,
                         uint16_t port,
                         mdns_ip_addr_t* address,
                         mdns_txt_item_t* txt = nullptr,
                         size_t txt_count = 0,
                         uint8_t* txt_value_len = nullptr,
                         mdns_result_t* next = nullptr) {
    mdns_result_t result{};
    result.next = next;
    result.instance_name = instance_name;
    result.hostname = hostname;
    result.port = port;
    result.txt = txt;
    result.txt_count = txt_count;
    result.txt_value_len = txt_value_len;
    result.addr = address;
    return result;
}

std::vector<std::string> CandidateUrls(const std::vector<MdnsGatewayCandidate>& candidates) {
    std::vector<std::string> urls;
    for (const auto& candidate : candidates) {
        urls.push_back(candidate.url);
    }
    return urls;
}

std::vector<std::string> CandidatePaths(const std::vector<MdnsGatewayCandidate>& candidates) {
    std::vector<std::string> paths;
    for (const auto& candidate : candidates) {
        paths.push_back(candidate.path);
    }
    return paths;
}

TEST(MdnsCandidateExtractTest, AccumulatesCandidatesFromMultipleInstances) {
    mdns_txt_item_t txt_one[] = {{"version", "1"}, {"path", "/one"}};
    uint8_t txt_one_lengths[] = {1, 4};
    auto addr_one = MdnsAddress(Ipv4(192, 168, 0, 10));
    auto result_one = MdnsResult("gateway-one", "host-one", 8765, &addr_one,
                                 txt_one, 2, txt_one_lengths);

    mdns_txt_item_t txt_two[] = {{"version", "1"}, {"path", "/two"}};
    uint8_t txt_two_lengths[] = {1, 4};
    auto addr_two = MdnsAddress(Ipv4(192, 168, 0, 11));
    auto result_two = MdnsResult("gateway-two", "host-two", 8766, &addr_two,
                                 txt_two, 2, txt_two_lengths);
    result_one.next = &result_two;

    auto extracted = ExtractGatewayCandidatesFromMdnsResults(&result_one,
                                                             CountResults(&result_one));

    EXPECT_EQ(extracted.accepted_instances, 2);
    ASSERT_EQ(extracted.candidates.size(), 2U);
    EXPECT_EQ(extracted.candidates[0].instance_name, "gateway-one");
    EXPECT_EQ(extracted.candidates[1].instance_name, "gateway-two");
    EXPECT_EQ(CandidateUrls(extracted.candidates),
              (std::vector<std::string>{
                  "ws://192.168.0.10:8765/one",
                  "ws://192.168.0.11:8766/two",
              }));
}

TEST(MdnsCandidateExtractTest, SkipsUnsupportedTxtVersion) {
    mdns_txt_item_t txt[] = {{"version", "2"}};
    uint8_t txt_lengths[] = {1};
    auto address = MdnsAddress(Ipv4(192, 168, 0, 20));
    auto result = MdnsResult("gateway", "host", 8765, &address, txt, 1, txt_lengths);

    auto extracted = ExtractGatewayCandidatesFromMdnsResults(&result, CountResults(&result));

    EXPECT_EQ(extracted.accepted_instances, 0);
    EXPECT_TRUE(extracted.candidates.empty());
}

TEST(MdnsCandidateExtractTest, SkipsZeroPort) {
    mdns_txt_item_t txt[] = {{"version", "1"}};
    uint8_t txt_lengths[] = {1};
    auto address = MdnsAddress(Ipv4(192, 168, 0, 30));
    auto result = MdnsResult("gateway", "host", 0, &address, txt, 1, txt_lengths);

    auto extracted = ExtractGatewayCandidatesFromMdnsResults(&result, CountResults(&result));

    EXPECT_EQ(extracted.accepted_instances, 0);
    EXPECT_TRUE(extracted.candidates.empty());
}

TEST(MdnsCandidateExtractTest, SkipsIpv6OnlyResult) {
    auto address = MdnsAddress(Ipv6());
    auto result = MdnsResult("gateway", "host", 8765, &address);

    auto extracted = ExtractGatewayCandidatesFromMdnsResults(&result, CountResults(&result));

    EXPECT_EQ(extracted.accepted_instances, 0);
    EXPECT_TRUE(extracted.candidates.empty());
}

TEST(MdnsCandidateExtractTest, NormalizesPathTxtForms) {
    auto addr_missing = MdnsAddress(Ipv4(192, 168, 1, 10));
    mdns_txt_item_t txt_empty[] = {{"path", ""}};
    uint8_t txt_empty_lengths[] = {0};
    auto addr_empty = MdnsAddress(Ipv4(192, 168, 1, 11));
    auto result_empty = MdnsResult("empty", "host-empty", 8765, &addr_empty,
                                   txt_empty, 1, txt_empty_lengths);

    mdns_txt_item_t txt_relative[] = {{"path", "api"}};
    uint8_t txt_relative_lengths[] = {3};
    auto addr_relative = MdnsAddress(Ipv4(192, 168, 1, 12));
    auto result_relative = MdnsResult("relative", "host-relative", 8765, &addr_relative,
                                      txt_relative, 1, txt_relative_lengths);

    mdns_txt_item_t txt_root[] = {{"path", "/"}};
    uint8_t txt_root_lengths[] = {1};
    auto addr_root = MdnsAddress(Ipv4(192, 168, 1, 13));
    auto result_root = MdnsResult("root", "host-root", 8765, &addr_root,
                                  txt_root, 1, txt_root_lengths);

    mdns_txt_item_t txt_nested[] = {{"path", "/api/v1"}};
    uint8_t txt_nested_lengths[] = {7};
    auto addr_nested = MdnsAddress(Ipv4(192, 168, 1, 14));
    auto result_nested = MdnsResult("nested", "host-nested", 8765, &addr_nested,
                                    txt_nested, 1, txt_nested_lengths);

    auto result_missing = MdnsResult("missing", "host-missing", 8765, &addr_missing);
    result_missing.next = &result_empty;
    result_empty.next = &result_relative;
    result_relative.next = &result_root;
    result_root.next = &result_nested;

    auto extracted = ExtractGatewayCandidatesFromMdnsResults(&result_missing,
                                                             CountResults(&result_missing));

    EXPECT_EQ(extracted.accepted_instances, 5);
    ASSERT_EQ(extracted.candidates.size(), 5U);
    EXPECT_EQ(CandidatePaths(extracted.candidates),
              (std::vector<std::string>{"/", "/", "/api", "/", "/api/v1"}));
    EXPECT_EQ(CandidateUrls(extracted.candidates),
              (std::vector<std::string>{
                  "ws://192.168.1.10:8765/",
                  "ws://192.168.1.11:8765/",
                  "ws://192.168.1.12:8765/api",
                  "ws://192.168.1.13:8765/",
                  "ws://192.168.1.14:8765/api/v1",
              }));
}

TEST(MdnsCandidateExtractTest, PropagatesRawResultCountAndAcceptedInstanceCountSeparately) {
    auto secondary_address = MdnsAddress(Ipv4(192, 168, 2, 11));
    auto primary_address = MdnsAddress(Ipv4(192, 168, 2, 10), &secondary_address);
    auto accepted_one = MdnsResult("accepted-one", "host-one", 8765, &primary_address);

    mdns_txt_item_t skipped_txt[] = {{"version", "2"}};
    uint8_t skipped_txt_lengths[] = {1};
    auto skipped_address = MdnsAddress(Ipv4(192, 168, 2, 12));
    auto skipped = MdnsResult("skipped", "host-skipped", 8765, &skipped_address,
                              skipped_txt, 1, skipped_txt_lengths);

    auto accepted_two_address = MdnsAddress(Ipv4(192, 168, 2, 13));
    auto accepted_two = MdnsResult("accepted-two", "host-two", 8766, &accepted_two_address);
    accepted_one.next = &skipped;
    skipped.next = &accepted_two;

    auto extracted = ExtractGatewayCandidatesFromMdnsResults(&accepted_one,
                                                             CountResults(&accepted_one));

    EXPECT_EQ(CountResults(&accepted_one), 3);
    EXPECT_EQ(extracted.accepted_instances, 2);
    ASSERT_EQ(extracted.candidates.size(), 3U);
    for (const auto& candidate : extracted.candidates) {
        EXPECT_EQ(candidate.result_count, 3);
    }
    EXPECT_EQ(CandidateUrls(extracted.candidates),
              (std::vector<std::string>{
                  "ws://192.168.2.10:8765/",
                  "ws://192.168.2.11:8765/",
                  "ws://192.168.2.13:8766/",
              }));
}

}  // namespace
