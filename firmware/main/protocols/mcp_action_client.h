#ifndef MCP_ACTION_CLIENT_H
#define MCP_ACTION_CLIENT_H

#include "mcp_action_reconnect_policy.h"

#include <web_socket.h>
#include <esp_timer.h>

#include <atomic>
#include <memory>
#include <string>

// A second, action-only MCP transport for the Kimito integration.
//
// The upstream Protocol instance in Application is the authoritative
// AI.AGENT/Xiaozhi voice transport. This class must therefore never start or
// stop recording, play TTS, or mutate the device conversation state. It only
// carries JSON MCP envelopes and routes the resulting tool replies back to
// the same WebSocket session.
class McpActionClient {
public:
    McpActionClient();
    ~McpActionClient();

    // Start the optional action transport. An empty URL disables it. Failed
    // boot connections and later disconnects retry with bounded backoff.
    bool Start();
    bool IsConnected() const;

private:
    enum class TimerPurpose : int {
        kNone = 0,
        kReconnect,
        kHandshakeTimeout,
    };

    std::shared_ptr<std::atomic<bool>> alive_ =
        std::make_shared<std::atomic<bool>>(true);
    std::unique_ptr<WebSocket> websocket_;
    std::shared_ptr<std::atomic<bool>> current_notify_disconnect_;
    std::atomic<bool> connected_ = false;
    std::atomic<bool> timer_armed_ = false;
    std::atomic<TimerPurpose> timer_purpose_ = TimerPurpose::kNone;
    esp_timer_handle_t timer_ = nullptr;
    McpActionReconnectPolicy reconnect_policy_;
    std::string session_id_;

    bool ConnectOnce();
    std::string GetHelloMessage() const;
    // Decode the gateway envelope. `mcp` payloads are delegated to the common
    // McpServer, while audio/session messages are intentionally ignored so the
    // action gateway cannot compete with AI.AGENT for the microphone/speaker.
    void HandleText(const char* data, size_t len);
    void SendMcpReply(const std::string& payload);
    void ScheduleReconnect();
    void ScheduleHandshakeTimeout();
    bool ArmTimer(TimerPurpose purpose, uint32_t delay_ms);
    void StopTimer();
    void ResetSocket();
    void Shutdown();
};

#endif
