# ESP32 Wi-Fi Connect

This component helps with Wi-Fi connection for the device.

It first tries to connect to a Wi-Fi network using the credentials stored in the flash. If this fails, it starts an access point and a web server to allow the user to connect to a Wi-Fi network.

The URL to access the web server is `http://192.168.4.1`.

### Screenshot: Wi-Fi Configuration

<img src="assets/ap_v3.png" width="320" alt="Wi-Fi Configuration">

### Screenshot: Advanced Options

<img src="assets/ap_v3_advanced.png" width="320" alt="Advanced Configuration">

## Changelog: v3.1.0

- Event callback now includes an additional `data` parameter for extra information.
- Disconnected event now provides the disconnect reason code via the `data` parameter.
- This allows applications to handle different disconnect scenarios appropriately.

## Changelog: v3.0.0

- Added WifiManager class for unified WiFi connection management.
- Improved DnsServer and WifiConfigurationAp classes for better resource handling.
- Updated HTML for configuration success message to use exit endpoint instead of reboot.
- Enhanced error handling and state management in WifiStation.
- Cleaned up unused code and improved thread safety across components.

## Changelog: v2.6.0

- Add support for ESP32C5 5G mode.

## Changelog: v2.4.0

- Add ja / zh-TW languages.
- Add advanced tab.
- Add "Connection: close" headers to save open sockets.

## Changelog: v2.3.0

- Add support for language request.

## Changelog: v2.2.0

- Add support for ESP32 SmartConfig(ESPTouch v2)

## Changelog: v2.1.0

- Improve Wi-Fi connection logic.

## Changelog: v2.0.0

- Add support for multiple Wi-Fi SSID management.
- Auto switch to the best Wi-Fi network.
- Captive portal for Wi-Fi configuration.
- Support for multiple languages (English, Chinese).

## Configuration

The Wi-Fi credentials are stored in the flash under the "wifi" namespace.

The keys are "ssid", "ssid1", "ssid2" ... "ssid9", "password", "password1", "password2" ... "password9".

## Usage

```cpp
#include <wifi_manager.h>
#include <ssid_manager.h>

// Initialize the default event loop
ESP_ERROR_CHECK(esp_event_loop_create_default());

// Initialize NVS flash for Wi-Fi configuration
esp_err_t ret = nvs_flash_init();
if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    ret = nvs_flash_init();
}
ESP_ERROR_CHECK(ret);

// Get the WifiManager singleton
auto& wifi_manager = WifiManager::GetInstance();

// Initialize with configuration
WifiManagerConfig config;
config.ssid_prefix = "ESP32";  // AP mode SSID prefix
config.language = "zh-CN";     // Web UI language
wifi_manager.Initialize(config);

// Set event callback to handle WiFi events
// The callback receives the event type and optional data (e.g., disconnect reason)
wifi_manager.SetEventCallback([](WifiEvent event, const std::string& data) {
    switch (event) {
        case WifiEvent::Scanning:
            ESP_LOGI("WiFi", "Scanning for networks...");
            break;
        case WifiEvent::Connecting:
            ESP_LOGI("WiFi", "Connecting to network...");
            break;
        case WifiEvent::Connected:
            ESP_LOGI("WiFi", "Connected successfully!");
            break;
        case WifiEvent::Disconnected:
            // data contains the disconnect reason code
            ESP_LOGW("WiFi", "Disconnected from network, reason: %s", data.c_str());
            break;
        case WifiEvent::ConfigModeEnter:
            ESP_LOGI("WiFi", "Entered config mode");
            break;
        case WifiEvent::ConfigModeExit:
            ESP_LOGI("WiFi", "Exited config mode");
            break;
    }
});

// Check if there are saved Wi-Fi credentials
auto& ssid_list = SsidManager::GetInstance().GetSsidList();
if (ssid_list.empty()) {
    // No credentials saved, start config AP mode
    wifi_manager.StartConfigAp();
} else {
    // Try to connect to the saved Wi-Fi network
    wifi_manager.StartStation();
}
```

Please check https://github.com/78/xiaozhi-esp32 for more usage.
