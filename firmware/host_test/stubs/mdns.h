#ifndef STACKCHAN_HOST_TEST_MDNS_H_
#define STACKCHAN_HOST_TEST_MDNS_H_

// Minimal ESP-IDF v5.5.2 mdns.h stub for host tests.
// Mirrors only fields used by protocols/mdns_candidate_extract.cc.

#include "esp_netif_ip_addr.h"

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mdns_txt_item {
    const char* key;
    const char* value;
} mdns_txt_item_t;

typedef struct mdns_ip_addr {
    esp_ip_addr_t addr;
    struct mdns_ip_addr* next;
} mdns_ip_addr_t;

typedef struct mdns_result {
    struct mdns_result* next;
    const char* instance_name;
    const char* hostname;
    uint16_t port;
    mdns_txt_item_t* txt;
    size_t txt_count;
    uint8_t* txt_value_len;
    mdns_ip_addr_t* addr;
} mdns_result_t;

#ifdef __cplusplus
}
#endif

#endif  // STACKCHAN_HOST_TEST_MDNS_H_
