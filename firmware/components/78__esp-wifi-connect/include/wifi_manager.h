/*
 * WiFi Manager - Unified WiFi connection management
 * 
 * Thread Safety:
 * - All public methods are thread-safe (protected by internal mutex)
 * - Event callback is invoked from WiFi event task
 * 
 * Usage:
 *   auto& wifi = WifiManager::GetInstance();
 *   
 *   EventGroupHandle_t events = xEventGroupCreate();
 *   wifi.SetEventCallback([events](WifiEvent e) {
 *       if (e == WifiEvent::Connected) xEventGroupSetBits(events, BIT0);
 *       if (e == WifiEvent::ConfigModeExit) xEventGroupSetBits(events, BIT1);
 *   });
 *   
 *   wifi.Initialize(config);
 *   wifi.StartStation();
 *   xEventGroupWaitBits(events, BIT0 | BIT1, pdTRUE, pdFALSE, portMAX_DELAY);
 */

#ifndef _WIFI_MANAGER_H_
#define _WIFI_MANAGER_H_

#include <string>
#include <memory>
#include <functional>
#include <mutex>

#include "wifi_station.h"

class WifiStation;
class WifiConfigurationAp;

// WiFi events
enum class WifiEvent {
    Scanning,          // Started scanning for networks
    Connecting,        // Connecting to network (call GetSsid() for target)
    Connected,         // Successfully connected
    Disconnected,      // Disconnected from network
    ConfigModeEnter,   // Entered config AP mode
    ConfigModeExit,    // Exited config AP mode
};

// Configuration
struct WifiManagerConfig {
    std::string ssid_prefix = "ESP32";    // AP mode SSID prefix
    std::string language = "zh-CN";       // Web UI language
    
    // Station mode scan interval with exponential backoff
    int station_scan_min_interval_seconds = 10;   // Initial scan interval (fast retry)
    int station_scan_max_interval_seconds = 300;  // Maximum scan interval (5 minutes)
};

/**
 * WifiManager - Singleton for WiFi management
 */
class WifiManager {
public:
    static WifiManager& GetInstance();

    // ==================== Lifecycle ====================
    
    bool Initialize(const WifiManagerConfig& config = WifiManagerConfig{});
    bool IsInitialized() const;

    // ==================== Station Mode ====================
    
    void StartStation();   // Non-blocking, auto-stops config AP if active
    void StopStation();    // Non-blocking
    
    bool IsConnected() const;
    std::string GetSsid() const;
    std::string GetIpAddress() const;
    int GetRssi() const;
    int GetChannel() const;
    std::string GetMacAddress() const;

    // ==================== Config AP Mode ====================
    
    void StartConfigAp();  // Non-blocking, auto-stops station if active
    void StopConfigAp();   // Non-blocking
    
    bool IsConfigMode() const;
    std::string GetApSsid() const;
    std::string GetApWebUrl() const;

    // ==================== Power ====================
    
    void SetPowerSaveLevel(WifiPowerSaveLevel level);

    // ==================== Event ====================
    
    void SetEventCallback(std::function<void(WifiEvent, const std::string&)> callback);

    const WifiManagerConfig& GetConfig() const { return config_; }

    WifiManager(const WifiManager&) = delete;
    WifiManager& operator=(const WifiManager&) = delete;

private:
    WifiManager();
    ~WifiManager();

    void NotifyEvent(WifiEvent event, const std::string& data = "");

    WifiManagerConfig config_;
    std::unique_ptr<WifiStation> station_;
    std::unique_ptr<WifiConfigurationAp> config_ap_;

    mutable std::mutex mutex_;
    bool initialized_ = false;
    bool station_active_ = false;
    bool config_mode_active_ = false;

    std::function<void(WifiEvent, const std::string&)> event_callback_;
    mutable std::string mac_address_;
};

#endif // _WIFI_MANAGER_H_
