#ifndef STACKCHAN_HOST_TEST_ESP_NETIF_IP_ADDR_H_
#define STACKCHAN_HOST_TEST_ESP_NETIF_IP_ADDR_H_

// Minimal ESP-IDF v5.5.2 esp_netif_ip_addr.h stub for host tests.
// Mirrors only fields/macros used by protocols/mdns_candidate_extract.cc.

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    ESP_IPADDR_TYPE_V4 = 0,
    ESP_IPADDR_TYPE_V6 = 6,
    ESP_IPADDR_TYPE_ANY = 46,
} esp_ip_addr_type_t;

typedef struct esp_ip4_addr {
    uint32_t addr;
} esp_ip4_addr_t;

typedef struct esp_ip6_addr {
    uint32_t addr[4];
    uint8_t zone;
} esp_ip6_addr_t;

typedef struct esp_ip_addr {
    union {
        esp_ip6_addr_t ip6;
        esp_ip4_addr_t ip4;
    } u_addr;
    esp_ip_addr_type_t type;
} esp_ip_addr_t;

#define IPSTR "%d.%d.%d.%d"
#define IP2STR(ipaddr)                           \
    (int)((((ipaddr)->addr) >> 24) & 0xff),      \
        (int)((((ipaddr)->addr) >> 16) & 0xff),  \
        (int)((((ipaddr)->addr) >> 8) & 0xff),   \
        (int)(((ipaddr)->addr) & 0xff)

#ifdef __cplusplus
}
#endif

#endif  // STACKCHAN_HOST_TEST_ESP_NETIF_IP_ADDR_H_
