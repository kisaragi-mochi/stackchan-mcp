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
    // Per-socket "this disconnect should fire the reconnect path" flag.
    // The candidate loop in OpenAudioChannelInternal() creates a fresh
    // shared_ptr<atomic<bool>>(false) for each socket and captures it
    // into that socket's OnDisconnected lambda. ParseServerHello() flips
    // it to true the moment the server hello arrives (before the wait
    // bit is set, so a near-simultaneous close still observes an armed
    // flag). The same shared_ptr is mirrored here so any path that
    // intentionally tears down the socket (CloseAudioChannel, the
    // destructive prologue of OpenAudioChannelInternal, the destructor)
    // can flip it back to false right before calling websocket_.reset().
    // The lambda then observes the flag synchronously when the underlying
    // close fires and short-circuits without scheduling a reconnect.
    // Using std::atomic<bool> inside the shared_ptr makes the cross-task
    // read/write well-defined (the lambda runs on the WS task; the disarm
    // path runs on the main task).
    std::shared_ptr<std::atomic<bool>> current_notify_disconnect_;
    // Latch flipped by every code path that intentionally tears the
    // current socket down. Cleared the moment a fresh server hello is
    // acked. Checked by the deferred reconnect job that the timer
    // callback re-posts onto the main task — without this, a reconnect
    // job already enqueued before CloseAudioChannel() ran would fire
    // and re-open the channel against the user's intent (the timer's
    // own esp_timer_stop() does not cancel work the timer has already
    // re-posted via Application::Schedule).
    std::atomic<bool> intentional_close_ = false;
    int reconnect_interval_ms_ = WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS;
    int version_ = 1;

    void ParseServerHello(const cJSON* root,
                          const std::shared_ptr<std::atomic<bool>>& notify_disconnect);
    bool SendText(const std::string& text) override;
    std::string GetHelloMessage();
    bool OpenAudioChannelInternal(bool report_error);
    void ScheduleReconnect();
    void StopReconnectTimer();
};

#endif
