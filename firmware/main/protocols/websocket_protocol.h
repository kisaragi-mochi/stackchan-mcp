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
#define WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED (1 << 1)
#define WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS 5000
#define WEBSOCKET_RECONNECT_MAX_INTERVAL_MS 60000
// Keepalive: cadence at which the keepalive timer fires to check
// connection liveness and send an active WebSocket ping. Independent
// from the gateway-side `ping_interval` (which the firmware does not
// negotiate or observe).
#define WEBSOCKET_KEEPALIVE_INTERVAL_MS 15000
// Dead-connection threshold: if no data frame has arrived from the
// gateway within this many milliseconds, the connection is considered
// dead and a reconnect is forced. Sized to comfortably cover the
// gateway's normal MCP poll cadence (~30 s avatar_loader fetch) plus
// margin so a single missed poll does not trip the check.
#define WEBSOCKET_KEEPALIVE_DEAD_TIMEOUT_MS 60000

class WebsocketProtocol : public Protocol {
public:
    WebsocketProtocol();
    ~WebsocketProtocol();

    bool Start() override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    bool OpenAudioChannel() override;
    void CloseAudioChannel(bool send_goodbye = true) override;
    bool IsAudioChannelOpened() const override;
    bool IsTransportConnected() const override;

private:
    std::shared_ptr<std::atomic<bool>> alive_ = std::make_shared<std::atomic<bool>>(true);
    EventGroupHandle_t event_group_handle_;
    std::unique_ptr<WebSocket> websocket_;
    esp_timer_handle_t reconnect_timer_ = nullptr;
    // Periodic keepalive timer. Started after a successful WebSocket
    // handshake, stopped from OnDisconnected and the destructor. Fires
    // every WEBSOCKET_KEEPALIVE_INTERVAL_MS to (a) actively ping the
    // server and (b) check the time since the last received data frame
    // — forcing a reconnect if the path has been silent for longer than
    // WEBSOCKET_KEEPALIVE_DEAD_TIMEOUT_MS. Without this, a silent
    // mid-stream network break (e.g. a brief WiFi outage that drops
    // packets in both directions before any TCP FIN/RST can propagate)
    // leaves the device in a stuck-connected state with no recovery —
    // see issue #239.
    esp_timer_handle_t keepalive_timer_ = nullptr;
    // Microsecond timestamp of the most recent data frame received on
    // the current WebSocket. Updated in OnData (text/binary frames sent
    // by the gateway, including normal MCP polls). The keepalive timer
    // compares (now - last_received_us_) against the dead threshold to
    // detect silent path breaks.
    std::atomic<uint64_t> last_received_us_{0};
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
    // Logical audio-channel state, independent of the physical WebSocket
    // connection. Set to true after a successful server hello exchange
    // (ParseServerHello), set to false in CloseAudioChannel() and
    // OnDisconnected(). IsAudioChannelOpened() checks this flag instead
    // of the raw socket state so that keeping the WebSocket alive for
    // MCP control does not make callers (ToggleChatState,
    // CanEnterSleepMode) believe an audio session is still active.
    std::atomic<bool> audio_channel_open_ = false;
    // Physical WebSocket transport state, updated on the WS task (OnDisconnected)
    // and the main task (OpenAudioChannelInternal prologue + success exit +
    // destructor). IsTransportConnected() returns this flag and is read from
    // Application::CanEnterSleepMode(), which runs on ESP_TIMER_TASK — atomic
    // so the timer-task read does not race with main-task websocket_.reset()
    // in OpenAudioChannelInternal / destructor / reconnect paths.
    std::atomic<bool> transport_connected_ = false;
    int reconnect_interval_ms_ = WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS;
    int version_ = 1;

    void ParseServerHello(const cJSON* root,
                          const std::shared_ptr<std::atomic<bool>>& notify_disconnect,
                          bool arm_audio_channel);
    bool SendText(const std::string& text) override;
    std::string GetHelloMessage();
    bool OpenAudioChannelInternal(bool report_error, bool arm_audio_channel = true);
    void ScheduleReconnect();
    void StopReconnectTimer();
    void StartKeepaliveTimer();
    void StopKeepaliveTimer();
    void OnKeepaliveTick();
};

#endif
