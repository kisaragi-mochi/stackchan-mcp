#ifndef _WEBSOCKET_PROTOCOL_H_
#define _WEBSOCKET_PROTOCOL_H_


#include "protocol.h"

#include <web_socket.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <esp_timer.h>

#include <atomic>
#include <memory>

#define WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT (1 << 0)
#define WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS 5000
#define WEBSOCKET_RECONNECT_MAX_INTERVAL_MS 60000

class WebsocketProtocol : public Protocol {
public:
    WebsocketProtocol();
    ~WebsocketProtocol();

    bool Start() override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    bool OpenAudioChannel() override;
    void CloseAudioChannel(bool send_goodbye = true) override;
    bool IsAudioChannelOpened() const override;

private:
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    EventGroupHandle_t event_group_handle_;
    std::unique_ptr<WebSocket> websocket_;
    esp_timer_handle_t reconnect_timer_ = nullptr;
    std::atomic<bool> auto_reconnect_enabled_ = false;
    int reconnect_interval_ms_ = WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS;
    int version_ = 1;

    void ParseServerHello(const cJSON* root);
    bool SendText(const std::string& text) override;
    std::string GetHelloMessage();
    bool OpenAudioChannelInternal(bool report_error);
    void ScheduleReconnect();
    void StopReconnectTimer();
};

#endif
