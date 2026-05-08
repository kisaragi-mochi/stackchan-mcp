#ifndef _WIFI_CONFIGURATION_AP_H_
#define _WIFI_CONFIGURATION_AP_H_

#include <string>
#include <vector>
#include <mutex>
#include <memory>
#include <functional>

#include <esp_http_server.h>
#include <esp_event.h>
#include <esp_timer.h>
#include <esp_netif.h>
#include <esp_wifi_types_generic.h>

#include "dns_server.h"
#include "sdkconfig.h"

/**
 * WifiConfigurationAp - WiFi configuration access point
 * 
 * Creates a WiFi hotspot with a captive portal for configuring WiFi credentials.
 * Note: WiFi driver must be initialized before using this class.
 */
class WifiConfigurationAp {
public:
    WifiConfigurationAp();
    ~WifiConfigurationAp();

    // Delete copy constructor and assignment operator
    WifiConfigurationAp(const WifiConfigurationAp&) = delete;
    WifiConfigurationAp& operator=(const WifiConfigurationAp&) = delete;

    void SetSsidPrefix(const std::string &&ssid_prefix);
    void SetSsidPrefix(const std::string &ssid_prefix);
    void SetLanguage(const std::string &&language);
    void SetLanguage(const std::string &language);
    void Start();
    void Stop();
#if !CONFIG_IDF_TARGET_ESP32P4
    void StartSmartConfig();
#endif
    bool ConnectToWifi(const std::string &ssid, const std::string &password);
    void Save(const std::string &ssid, const std::string &password);
    std::vector<wifi_ap_record_t> GetAccessPoints();
    std::string GetSsid();
    std::string GetWebServerUrl();

    /**
     * Set callback for when exit is requested from config mode
     * This is called when user requests to exit config mode (e.g., via /exit endpoint)
     */
    void OnExitRequested(std::function<void()> callback);

private:
    std::mutex mutex_;
    std::unique_ptr<DnsServer> dns_server_;
    httpd_handle_t server_ = NULL;
    EventGroupHandle_t event_group_;
    std::string ssid_prefix_;
    std::string language_;
    esp_event_handler_instance_t instance_any_id_;
    esp_event_handler_instance_t instance_got_ip_;
    esp_timer_handle_t scan_timer_ = nullptr;
    bool is_connecting_ = false;
    esp_netif_t* ap_netif_ = nullptr;
    std::vector<wifi_ap_record_t> ap_records_;

    // 高级配置项
    std::string ota_url_;
    // WebSocket gateway URL persisted in the "websocket" NVS namespace
    // (key "url"), readable from main/protocols/websocket_protocol.cc
    // through Settings("websocket").GetString("url"). Lets end users
    // running a pre-built firmware point the device at their stackchan-mcp
    // gateway from the on-device WiFi config UI without rebuilding.
    std::string websocket_url_;
    // Optional fallback WebSocket gateway URL persisted to the same
    // "websocket" NVS namespace (key "fallback_url"). The firmware
    // connection logic in websocket_protocol.cc tries the primary URL
    // first and the fallback after the primary candidate fails the
    // server-hello flow, so users can configure a "local primary +
    // remote fallback" gateway profile from the WiFi config UI.
    std::string websocket_fallback_url_;
    // Optional bearer token persisted to the same "websocket" NVS
    // namespace (key "token"). websocket_protocol.cc sends it in the
    // HTTP Authorization header when non-empty. Stored as a separate
    // field so it can be input via a password-style UI control without
    // exposing the value in the URL fields.
    std::string websocket_token_;
    int8_t max_tx_power_;
    bool remember_bssid_;
    bool sleep_mode_;

    // Callbacks
    std::function<void()> on_exit_requested_;

    void StartAccessPoint();
    void StartWebServer();

    // Event handlers
    static void WifiEventHandler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data);
    static void IpEventHandler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data);
#if !CONFIG_IDF_TARGET_ESP32P4
    static void SmartConfigEventHandler(void* arg, esp_event_base_t event_base, 
                                      int32_t event_id, void* event_data);
    esp_event_handler_instance_t sc_event_instance_ = nullptr;
#endif
};

#endif // _WIFI_CONFIGURATION_AP_H_
