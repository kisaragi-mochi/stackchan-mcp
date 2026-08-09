#include "mcp_action_client.h"

#include <cJSON.h>
#include <esp_log.h>
#include <esp_system.h>

#include "application.h"
#include "board.h"
#include "mcp_server.h"
#include "system_info.h"

#define TAG "MCP_ACTION"

// This client speaks the same outer hello/envelope convention as the
// stackchan-mcp gateway, but it is intentionally a separate WebSocket from
// the primary Xiaozhi connection. The gateway can therefore expose all
// registered device tools to Beam Pro, Unity, Kimito companion, or a test
// script without touching the audio pipeline.

namespace {

constexpr uint32_t kHandshakeTimeoutMs = 10000;

}  // namespace

McpActionClient::McpActionClient() {
    esp_timer_create_args_t timer_args = {
        .callback = [](void* arg) {
            auto* client = static_cast<McpActionClient*>(arg);
            auto alive = client->alive_;
            Application::GetInstance().Schedule([client, alive]() {
                if (!alive->load()) {
                    return;
                }
                client->timer_armed_.store(false);
                const auto purpose =
                    client->timer_purpose_.exchange(TimerPurpose::kNone);
                if (purpose == TimerPurpose::kReconnect) {
                    ESP_LOGI(TAG, "Retrying action MCP gateway connection");
                    client->ConnectOnce();
                } else if (purpose == TimerPurpose::kHandshakeTimeout &&
                           !client->connected_.load()) {
                    ESP_LOGW(TAG, "Action MCP server hello timed out");
                    client->ResetSocket();
                    client->ScheduleReconnect();
                }
            });
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "mcp_action_retry",
        .skip_unhandled_events = true,
    };
    if (esp_timer_create(&timer_args, &timer_) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create action MCP retry timer");
        timer_ = nullptr;
    }
}

McpActionClient::~McpActionClient() {
    Shutdown();
}

bool McpActionClient::Start() {
#ifdef CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL
    if (CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL[0] == '\0') {
        ESP_LOGI(TAG, "Action MCP gateway disabled (empty URL)");
        return false;
    }

    return ConnectOnce();
#else
    ESP_LOGI(TAG, "Action MCP gateway disabled (no URL configured)");
    return false;
#endif
}

bool McpActionClient::ConnectOnce() {
#ifdef CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL
    if (!alive_->load() || CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL[0] == '\0') {
        return false;
    }

    auto network = Board::GetInstance().GetNetwork();
    if (network == nullptr) {
        ESP_LOGW(TAG, "Network is unavailable; action MCP gateway retry scheduled");
        ScheduleReconnect();
        return false;
    }

    StopTimer();
    ResetSocket();
    // Socket index 2 is independent of the primary protocol socket. The
    // index is owned by the network implementation and must not be reused by
    // the AI.AGENT transport, otherwise callbacks can be delivered to the
    // wrong protocol object.
    websocket_ = network->CreateWebSocket(2);
    if (websocket_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create action MCP WebSocket");
        ScheduleReconnect();
        return false;
    }

#ifdef CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_TOKEN
    if (CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_TOKEN[0] != '\0') {
        std::string token = "Bearer ";
        token += CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_TOKEN;
        websocket_->SetHeader("Authorization", token.c_str());
    }
#endif
    websocket_->SetHeader("Protocol-Version", "1");
    websocket_->SetHeader("Device-Id", SystemInfo::GetMacAddress().c_str());
    websocket_->SetHeader("Client-Id", Board::GetInstance().GetUuid().c_str());
    websocket_->OnData([this](const char* data, size_t len, bool binary) {
        if (binary) {
            ESP_LOGW(TAG, "Ignoring unexpected binary frame from action gateway");
            return;
        }
        HandleText(data, len);
    });
    // Keep reconnect notification disarmed until Connect + hello-send have
    // succeeded. Some implementations synchronously fire OnDisconnected from
    // a failed Connect(); that failure is already handled below and must not
    // consume a second backoff step through the callback path.
    auto notify_disconnect = std::make_shared<std::atomic<bool>>(false);
    current_notify_disconnect_ = notify_disconnect;
    websocket_->OnDisconnected([this, notify_disconnect]() {
        connected_.store(false);
        ESP_LOGW(TAG, "Action MCP gateway disconnected");
        if (!alive_->load() ||
            !notify_disconnect->exchange(false, std::memory_order_acq_rel)) {
            return;
        }
        auto alive = alive_;
        Application::GetInstance().Schedule([this, alive]() {
            if (!alive->load()) {
                return;
            }
            ResetSocket();
            ScheduleReconnect();
        });
    });

    ESP_LOGI(TAG, "Connecting action MCP gateway: %s",
             CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL);
    if (!websocket_->Connect(CONFIG_STACKCHAN_MCP_ACTION_GATEWAY_URL)) {
        ESP_LOGE(TAG, "Action MCP gateway connection failed, code=%d",
                 websocket_->GetLastError());
        ResetSocket();
        ScheduleReconnect();
        return false;
    }
    // The hello advertises MCP only. It deliberately does not advertise an
    // audio contract for this connection; any incoming `tts`/`listen` event
    // is rejected later because AI.AGENT is the sole audio owner.
    auto hello = GetHelloMessage();
    if (!websocket_->Send(hello)) {
        ESP_LOGE(TAG, "Failed to send action MCP hello");
        ResetSocket();
        ScheduleReconnect();
        return false;
    }
    notify_disconnect->store(true, std::memory_order_release);
    ScheduleHandshakeTimeout();
    return true;
#else
    return false;
#endif
}

bool McpActionClient::IsConnected() const {
    return connected_.load();
}

std::string McpActionClient::GetHelloMessage() const {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "hello");
    cJSON_AddNumberToObject(root, "version", 1);
    cJSON* features = cJSON_CreateObject();
    cJSON_AddBoolToObject(features, "mcp", true);
    cJSON_AddItemToObject(root, "features", features);
    cJSON_AddStringToObject(root, "transport", "websocket");
    cJSON* audio = cJSON_CreateObject();
    cJSON_AddStringToObject(audio, "format", "opus");
    cJSON_AddNumberToObject(audio, "sample_rate", 16000);
    cJSON_AddNumberToObject(audio, "channels", 1);
    cJSON_AddNumberToObject(audio, "frame_duration", 60);
    cJSON_AddItemToObject(root, "audio_params", audio);
    char* text = cJSON_PrintUnformatted(root);
    std::string result = text == nullptr ? "" : text;
    if (text != nullptr) {
        cJSON_free(text);
    }
    cJSON_Delete(root);
    return result;
}

void McpActionClient::HandleText(const char* data, size_t len) {
    cJSON* root = cJSON_ParseWithLength(data, len);
    if (root == nullptr) {
        ESP_LOGW(TAG, "Invalid JSON from action gateway");
        return;
    }
    auto type = cJSON_GetObjectItem(root, "type");
    if (cJSON_IsString(type) && strcmp(type->valuestring, "hello") == 0) {
        auto session = cJSON_GetObjectItem(root, "session_id");
        if (cJSON_IsString(session) && session->valuestring[0] != '\0') {
            session_id_ = session->valuestring;
            connected_.store(true);
            auto alive = alive_;
            Application::GetInstance().Schedule([this, alive]() {
                if (!alive->load() || !connected_.load()) {
                    return;
                }
                StopTimer();
                reconnect_policy_.Reset();
            });
            ESP_LOGI(TAG, "Action MCP gateway connected, session=%s",
                     session_id_.c_str());
        }
    } else if (cJSON_IsString(type) && strcmp(type->valuestring, "mcp") == 0) {
        auto payload = cJSON_GetObjectItem(root, "payload");
        if (cJSON_IsObject(payload)) {
            // ParseMessage receives a callback bound to this client. Without
            // this callback McpServer would use its legacy primary-channel
            // sender and the response would be returned to Xiaozhi instead
            // of the action gateway that issued the request.
            McpServer::GetInstance().ParseMessage(
                payload,
                [this](const std::string& reply) { SendMcpReply(reply); });
        }
    } else if (cJSON_IsString(type) &&
               (strcmp(type->valuestring, "tts") == 0 ||
                strcmp(type->valuestring, "listen") == 0)) {
        // Do not create a second audio/session state machine here. This is a
        // safety boundary, not merely an unsupported-message warning.
        ESP_LOGW(TAG, "Ignoring action gateway audio event '%s'; AI.AGENT owns audio",
                 type->valuestring);
    }
    cJSON_Delete(root);
}

void McpActionClient::SendMcpReply(const std::string& payload) {
    Application::GetInstance().Schedule([this, payload]() {
        if (!IsConnected()) {
            ESP_LOGW(TAG, "Dropping MCP reply because action gateway is offline");
            return;
        }
        // Sending is scheduled on Application's main task because the ESP
        // WebSocket implementation and the existing MCP path are not safe to
        // call concurrently from an arbitrary network callback.
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "type", "mcp");
        cJSON_AddStringToObject(root, "session_id", session_id_.c_str());
        cJSON* json_payload = cJSON_Parse(payload.c_str());
        if (json_payload == nullptr) {
            ESP_LOGE(TAG, "Cannot wrap invalid MCP reply");
            cJSON_Delete(root);
            return;
        }
        cJSON_AddItemToObject(root, "payload", json_payload);
        char* text = cJSON_PrintUnformatted(root);
        if (text != nullptr) {
            websocket_->Send(std::string(text));
            cJSON_free(text);
        }
        cJSON_Delete(root);
    });
}

void McpActionClient::ScheduleReconnect() {
    if (!alive_->load() || timer_ == nullptr) {
        return;
    }
    bool expected = false;
    if (!timer_armed_.compare_exchange_strong(expected, true)) {
        ESP_LOGI(TAG, "Action MCP retry/handshake timer already armed");
        return;
    }
    const uint32_t delay_ms = reconnect_policy_.ConsumeDelayMs();
    timer_purpose_.store(TimerPurpose::kReconnect);
    const esp_err_t err = esp_timer_start_once(timer_, delay_ms * 1000ULL);
    if (err != ESP_OK) {
        timer_armed_.store(false);
        timer_purpose_.store(TimerPurpose::kNone);
        ESP_LOGW(TAG, "Failed to arm action MCP reconnect, err=%d", err);
        return;
    }
    ESP_LOGI(TAG, "Schedule action MCP reconnect in %u seconds",
             static_cast<unsigned>(delay_ms / 1000));
}

void McpActionClient::ScheduleHandshakeTimeout() {
    ArmTimer(TimerPurpose::kHandshakeTimeout, kHandshakeTimeoutMs);
}

bool McpActionClient::ArmTimer(TimerPurpose purpose, uint32_t delay_ms) {
    if (!alive_->load() || timer_ == nullptr) {
        return false;
    }
    StopTimer();
    timer_purpose_.store(purpose);
    timer_armed_.store(true);
    const esp_err_t err = esp_timer_start_once(timer_, delay_ms * 1000ULL);
    if (err != ESP_OK) {
        timer_armed_.store(false);
        timer_purpose_.store(TimerPurpose::kNone);
        ESP_LOGW(TAG, "Failed to arm action MCP timer, err=%d", err);
        return false;
    }
    return true;
}

void McpActionClient::StopTimer() {
    timer_armed_.store(false);
    timer_purpose_.store(TimerPurpose::kNone);
    if (timer_ == nullptr) {
        return;
    }
    const esp_err_t err = esp_timer_stop(timer_);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "Failed to stop action MCP timer, err=%d", err);
    }
}

void McpActionClient::ResetSocket() {
    // A socket reset invalidates both its pending server-hello deadline and
    // any retry already associated with that socket. The caller chooses
    // whether to arm a fresh reconnect after teardown.
    StopTimer();
    connected_.store(false);
    session_id_.clear();
    if (current_notify_disconnect_) {
        current_notify_disconnect_->store(false);
        current_notify_disconnect_.reset();
    }
    if (websocket_ != nullptr) {
        websocket_->Close();
        websocket_.reset();
    }
}

void McpActionClient::Shutdown() {
    alive_->store(false);
    StopTimer();
    ResetSocket();
    if (timer_ != nullptr) {
        esp_timer_delete(timer_);
        timer_ = nullptr;
    }
}
