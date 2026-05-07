#ifndef _DNS_SERVER_H_
#define _DNS_SERVER_H_

#include <string>
#include <atomic>
#include <esp_netif_ip_addr.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

class DnsServer {
public:
    DnsServer();
    ~DnsServer();

    void Start(esp_ip4_addr_t gateway);
    void Stop();

private:
    int port_ = 53;
    int fd_ = -1;
    esp_ip4_addr_t gateway_;
    std::atomic<bool> running_{false};
    TaskHandle_t task_handle_ = nullptr;
    void Run();
};

#endif // _DNS_SERVER_H_
