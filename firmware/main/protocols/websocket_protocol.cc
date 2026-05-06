#include "websocket_protocol.h"
#include "board.h"
#include "system_info.h"
#include "application.h"
#include "settings.h"

#include <cstring>
#include <cJSON.h>
#include <esp_log.h>
#include <arpa/inet.h>
#include <algorithm>
#include <vector>
#include "assets/lang_config.h"

#define TAG "WS"

namespace {

void AddGatewayCandidate(std::vector<std::string>& candidates, const std::string& url, const char* source) {
    if (url.empty()) {
        return;
    }
    if (std::find(candidates.begin(), candidates.end(), url) != candidates.end()) {
        ESP_LOGI(TAG, "Skipping duplicate websocket gateway candidate from %s: %s", source, url.c_str());
        return;
    }
    ESP_LOGI(TAG, "Adding websocket gateway candidate from %s: %s", source, url.c_str());
    candidates.push_back(url);
}

} // namespace

WebsocketProtocol::WebsocketProtocol() {
    event_group_handle_ = xEventGroupCreate();

    esp_timer_create_args_t reconnect_timer_args = {
        .callback = [](void* arg) {
            auto protocol = static_cast<WebsocketProtocol*>(arg);
            auto alive = protocol->alive_;
            Application::GetInstance().Schedule([protocol, alive]() {
                if (!*alive || !protocol->auto_reconnect_enabled_.load()) {
                    return;
                }

                auto& app = Application::GetInstance();
                if (app.GetDeviceState() != kDeviceStateIdle) {
                    protocol->ScheduleReconnect();
                    return;
                }

                ESP_LOGI(TAG, "Reconnecting to websocket server");
                if (!protocol->OpenAudioChannelInternal(false)) {
                    protocol->ScheduleReconnect();
                }
            });
        },
        .arg = this,
    };
    esp_timer_create(&reconnect_timer_args, &reconnect_timer_);
}

WebsocketProtocol::~WebsocketProtocol() {
    *alive_ = false;
    StopReconnectTimer();
    if (reconnect_timer_ != nullptr) {
        esp_timer_delete(reconnect_timer_);
    }
    websocket_.reset();
    if (event_group_handle_ != nullptr) {
        vEventGroupDelete(event_group_handle_);
    }
}

bool WebsocketProtocol::Start() {
    // Only connect to server when audio channel is needed
    return true;
}

bool WebsocketProtocol::SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
    if (websocket_ == nullptr || !websocket_->IsConnected()) {
        return false;
    }

    if (version_ == 2) {
        std::string serialized;
        serialized.resize(sizeof(BinaryProtocol2) + packet->payload.size());
        auto bp2 = (BinaryProtocol2*)serialized.data();
        bp2->version = htons(version_);
        bp2->type = 0;
        bp2->reserved = 0;
        bp2->timestamp = htonl(packet->timestamp);
        bp2->payload_size = htonl(packet->payload.size());
        memcpy(bp2->payload, packet->payload.data(), packet->payload.size());

        return websocket_->Send(serialized.data(), serialized.size(), true);
    } else if (version_ == 3) {
        std::string serialized;
        serialized.resize(sizeof(BinaryProtocol3) + packet->payload.size());
        auto bp3 = (BinaryProtocol3*)serialized.data();
        bp3->type = 0;
        bp3->reserved = 0;
        bp3->payload_size = htons(packet->payload.size());
        memcpy(bp3->payload, packet->payload.data(), packet->payload.size());

        return websocket_->Send(serialized.data(), serialized.size(), true);
    } else {
        return websocket_->Send(packet->payload.data(), packet->payload.size(), true);
    }
}

bool WebsocketProtocol::SendText(const std::string& text) {
    if (websocket_ == nullptr || !websocket_->IsConnected()) {
        return false;
    }

    if (!websocket_->Send(text)) {
        ESP_LOGE(TAG, "Failed to send text: %s", text.c_str());
        SetError(Lang::Strings::SERVER_ERROR);
        return false;
    }

    return true;
}

bool WebsocketProtocol::IsAudioChannelOpened() const {
    return websocket_ != nullptr && websocket_->IsConnected() && !error_occurred_ && !IsTimeout();
}

void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {
    (void)send_goodbye;  // Websocket doesn't need to send goodbye message
    auto_reconnect_enabled_.store(false);
    StopReconnectTimer();
    websocket_.reset();
}

bool WebsocketProtocol::OpenAudioChannel() {
    return OpenAudioChannelInternal(true);
}

bool WebsocketProtocol::OpenAudioChannelInternal(bool report_error) {
    bool reconnect_was_enabled = auto_reconnect_enabled_.load();
    auto_reconnect_enabled_.store(false);
    StopReconnectTimer();
    websocket_.reset();
    auto_reconnect_enabled_.store(reconnect_was_enabled);
    session_id_ = "";
    xEventGroupClearBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);

    Settings settings("websocket", false);
    // Read the gateway URL from NVS (set via the WiFi config UI's "websocket
    // url" field on first boot, e.g. "ws://<your-gateway-lan-ip>:8765"). The
    // legacy OTA-config code path is disabled in application.cc by design —
    // this firmware always speaks to a stackchan-mcp gateway directly.
    std::string url = settings.GetString("url");
#ifdef CONFIG_DEFAULT_WEBSOCKET_URL
#ifdef CONFIG_FORCE_DEFAULT_WEBSOCKET_URL
    // Force mode: Kconfig URL always wins over NVS. Used when NVS contains
    // a stale upstream URL (e.g. wss://api.tenclass.net/...) that no
    // runtime tool can currently overwrite. Only forces when the Kconfig
    // value is non-empty so an unset Kconfig still falls through to NVS.
    if (CONFIG_DEFAULT_WEBSOCKET_URL[0] != '\0') {
        if (!url.empty() && url != CONFIG_DEFAULT_WEBSOCKET_URL) {
            ESP_LOGI(TAG,
                     "FORCE: overriding NVS websocket.url with Kconfig: NVS=%s -> %s",
                     url.c_str(), CONFIG_DEFAULT_WEBSOCKET_URL);
        } else if (url.empty()) {
            ESP_LOGI(TAG, "FORCE: using Kconfig websocket URL: %s", CONFIG_DEFAULT_WEBSOCKET_URL);
        }
        url = CONFIG_DEFAULT_WEBSOCKET_URL;
    }
#else
    if (url.empty()) {
        url = CONFIG_DEFAULT_WEBSOCKET_URL;
        if (!url.empty()) {
            ESP_LOGI(TAG, "NVS websocket.url empty; using build-time default from Kconfig: %s", url.c_str());
        }
    }
#endif
#endif
    std::vector<std::string> gateway_candidates;
    AddGatewayCandidate(gateway_candidates, url, "websocket.url");

    std::string fallback_url = settings.GetString("fallback_url");
#ifdef CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL
#ifdef CONFIG_FORCE_DEFAULT_WEBSOCKET_URL
    if (CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL[0] != '\0') {
        if (!fallback_url.empty() && fallback_url != CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL) {
            ESP_LOGI(TAG,
                     "FORCE: overriding NVS websocket.fallback_url with Kconfig: NVS=%s -> %s",
                     fallback_url.c_str(), CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL);
        } else if (fallback_url.empty()) {
            ESP_LOGI(TAG, "FORCE: using Kconfig fallback websocket URL: %s",
                     CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL);
        }
        fallback_url = CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL;
    }
#else
    if (fallback_url.empty()) {
        fallback_url = CONFIG_DEFAULT_WEBSOCKET_FALLBACK_URL;
        if (!fallback_url.empty()) {
            ESP_LOGI(TAG, "NVS websocket.fallback_url empty; using build-time fallback from Kconfig: %s",
                     fallback_url.c_str());
        }
    }
#endif
#endif
    AddGatewayCandidate(gateway_candidates, fallback_url, "websocket.fallback_url");

    std::string token = settings.GetString("token");
#ifdef CONFIG_DEFAULT_WEBSOCKET_TOKEN
#ifdef CONFIG_FORCE_DEFAULT_WEBSOCKET_URL
    // Same force-mode treatment for the token (same Kconfig switch
    // controls both, since URL and token are typically configured together).
    if (CONFIG_DEFAULT_WEBSOCKET_TOKEN[0] != '\0') {
        if (!token.empty() && token != CONFIG_DEFAULT_WEBSOCKET_TOKEN) {
            ESP_LOGI(TAG, "FORCE: overriding NVS websocket.token with Kconfig value");
        } else if (token.empty()) {
            ESP_LOGI(TAG, "FORCE: using Kconfig websocket token");
        }
        token = CONFIG_DEFAULT_WEBSOCKET_TOKEN;
    }
#else
    if (token.empty()) {
        token = CONFIG_DEFAULT_WEBSOCKET_TOKEN;
        if (!token.empty()) {
            ESP_LOGI(TAG, "NVS websocket.token empty; using build-time default from Kconfig");
        }
    }
#endif
#endif
    int version = settings.GetInt("version");
    if (version != 0) {
        version_ = version;
    }

    error_occurred_ = false;

    auto network = Board::GetInstance().GetNetwork();
    if (gateway_candidates.empty()) {
        ESP_LOGE(TAG, "No websocket gateway URL configured");
        if (report_error) {
            SetError(Lang::Strings::SERVER_NOT_CONNECTED);
        }
        return false;
    }

    if (!token.empty() && token.find(" ") == std::string::npos) {
        token = "Bearer " + token;
    }

    bool server_hello_timed_out = false;
    for (size_t i = 0; i < gateway_candidates.size(); ++i) {
        const auto& candidate_url = gateway_candidates[i];

        xEventGroupClearBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);
        websocket_ = network->CreateWebSocket(1);
        if (websocket_ == nullptr) {
            ESP_LOGE(TAG, "Failed to create websocket");
            continue;
        }
        auto notify_disconnect = std::make_shared<bool>(false);

        if (!token.empty()) {
            websocket_->SetHeader("Authorization", token.c_str());
        }
        websocket_->SetHeader("Protocol-Version", std::to_string(version_).c_str());
        websocket_->SetHeader("Device-Id", SystemInfo::GetMacAddress().c_str());
        websocket_->SetHeader("Client-Id", Board::GetInstance().GetUuid().c_str());

        websocket_->OnData([this](const char* data, size_t len, bool binary) {
            if (binary) {
                if (on_incoming_audio_ != nullptr) {
                    if (version_ == 2) {
                        BinaryProtocol2* bp2 = (BinaryProtocol2*)data;
                        bp2->version = ntohs(bp2->version);
                        bp2->type = ntohs(bp2->type);
                        bp2->timestamp = ntohl(bp2->timestamp);
                        bp2->payload_size = ntohl(bp2->payload_size);
                        auto payload = (uint8_t*)bp2->payload;
                        on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
                            .sample_rate = server_sample_rate_,
                            .frame_duration = server_frame_duration_,
                            .timestamp = bp2->timestamp,
                            .payload = std::vector<uint8_t>(payload, payload + bp2->payload_size)
                        }));
                    } else if (version_ == 3) {
                        BinaryProtocol3* bp3 = (BinaryProtocol3*)data;
                        bp3->type = bp3->type;
                        bp3->payload_size = ntohs(bp3->payload_size);
                        auto payload = (uint8_t*)bp3->payload;
                        on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
                            .sample_rate = server_sample_rate_,
                            .frame_duration = server_frame_duration_,
                            .timestamp = 0,
                            .payload = std::vector<uint8_t>(payload, payload + bp3->payload_size)
                        }));
                    } else {
                        on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
                            .sample_rate = server_sample_rate_,
                            .frame_duration = server_frame_duration_,
                            .timestamp = 0,
                            .payload = std::vector<uint8_t>((uint8_t*)data, (uint8_t*)data + len)
                        }));
                    }
                }
            } else {
                // Parse JSON data
                auto root = cJSON_ParseWithLength(data, len);
                auto type = cJSON_GetObjectItem(root, "type");
                if (cJSON_IsString(type)) {
                    if (strcmp(type->valuestring, "hello") == 0) {
                        ParseServerHello(root);
                    } else {
                        if (on_incoming_json_ != nullptr) {
                            on_incoming_json_(root);
                        }
                    }
                } else {
                    ESP_LOGE(TAG, "Missing message type, data: %s", std::string(data, len).c_str());
                }
                cJSON_Delete(root);
            }
            last_incoming_time_ = std::chrono::steady_clock::now();
        });

        websocket_->OnDisconnected([this, notify_disconnect]() {
            if (!*notify_disconnect) {
                ESP_LOGI(TAG, "Websocket candidate disconnected before server hello");
                return;
            }
            if (on_disconnected_ != nullptr) {
                on_disconnected_();
            }
            ESP_LOGI(TAG, "Websocket disconnected");
            if (on_audio_channel_closed_ != nullptr) {
                on_audio_channel_closed_();
            }
            if (auto_reconnect_enabled_.load()) {
                ScheduleReconnect();
            }
        });

        ESP_LOGI(TAG, "Connecting to websocket server candidate %d/%d: %s with version: %d",
                 static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()), candidate_url.c_str(), version_);
        if (!websocket_->Connect(candidate_url.c_str())) {
            ESP_LOGE(TAG, "Failed to connect to websocket server candidate %d/%d, code=%d",
                     static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()), websocket_->GetLastError());
            websocket_.reset();
            continue;
        }

        // Send hello message to describe the client
        auto message = GetHelloMessage();
        if (!websocket_->Send(message)) {
            ESP_LOGE(TAG, "Failed to send hello to websocket server candidate %d/%d",
                     static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()));
            websocket_.reset();
            continue;
        }

        // Wait for server hello
        EventBits_t bits = xEventGroupWaitBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT, pdTRUE, pdFALSE, pdMS_TO_TICKS(10000));
        if (!(bits & WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT)) {
            ESP_LOGE(TAG, "Failed to receive server hello from websocket server candidate %d/%d",
                     static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()));
            server_hello_timed_out = true;
            websocket_.reset();
            continue;
        }

        *notify_disconnect = true;
        auto_reconnect_enabled_.store(true);
        reconnect_interval_ms_ = WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS;
        StopReconnectTimer();

        if (on_connected_ != nullptr) {
            on_connected_();
        }

        if (on_audio_channel_opened_ != nullptr) {
            on_audio_channel_opened_();
        }

        ESP_LOGI(TAG, "Connected to websocket server candidate %d/%d: %s",
                 static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()), candidate_url.c_str());
        return true;
    }

    if (report_error) {
        if (server_hello_timed_out) {
            SetError(Lang::Strings::SERVER_TIMEOUT);
        } else {
            SetError(Lang::Strings::SERVER_NOT_CONNECTED);
        }
    }
    return false;
}

void WebsocketProtocol::ScheduleReconnect() {
    if (reconnect_timer_ == nullptr || !auto_reconnect_enabled_.load()) {
        return;
    }

    StopReconnectTimer();
    ESP_LOGI(TAG, "Schedule websocket reconnect in %d seconds", reconnect_interval_ms_ / 1000);
    esp_timer_start_once(reconnect_timer_, reconnect_interval_ms_ * 1000);
    reconnect_interval_ms_ = std::min(reconnect_interval_ms_ * 2, WEBSOCKET_RECONNECT_MAX_INTERVAL_MS);
}

void WebsocketProtocol::StopReconnectTimer() {
    if (reconnect_timer_ != nullptr) {
        esp_timer_stop(reconnect_timer_);
    }
}

std::string WebsocketProtocol::GetHelloMessage() {
    // keys: message type, version, audio_params (format, sample_rate, channels)
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "hello");
    cJSON_AddNumberToObject(root, "version", version_);
    cJSON* features = cJSON_CreateObject();
#if CONFIG_USE_SERVER_AEC
    cJSON_AddBoolToObject(features, "aec", true);
#endif
    cJSON_AddBoolToObject(features, "mcp", true);
    cJSON_AddItemToObject(root, "features", features);
    cJSON_AddStringToObject(root, "transport", "websocket");
    cJSON* audio_params = cJSON_CreateObject();
    cJSON_AddStringToObject(audio_params, "format", "opus");
    cJSON_AddNumberToObject(audio_params, "sample_rate", 16000);
    cJSON_AddNumberToObject(audio_params, "channels", 1);
    cJSON_AddNumberToObject(audio_params, "frame_duration", OPUS_FRAME_DURATION_MS);
    cJSON_AddItemToObject(root, "audio_params", audio_params);
    auto json_str = cJSON_PrintUnformatted(root);
    std::string message(json_str);
    cJSON_free(json_str);
    cJSON_Delete(root);
    return message;
}

void WebsocketProtocol::ParseServerHello(const cJSON* root) {
    auto transport = cJSON_GetObjectItem(root, "transport");
    if (transport == nullptr || strcmp(transport->valuestring, "websocket") != 0) {
        ESP_LOGE(TAG, "Unsupported transport: %s", transport->valuestring);
        return;
    }

    auto session_id = cJSON_GetObjectItem(root, "session_id");
    if (cJSON_IsString(session_id)) {
        session_id_ = session_id->valuestring;
        ESP_LOGI(TAG, "Session ID: %s", session_id_.c_str());
    }

    auto audio_params = cJSON_GetObjectItem(root, "audio_params");
    if (cJSON_IsObject(audio_params)) {
        auto sample_rate = cJSON_GetObjectItem(audio_params, "sample_rate");
        if (cJSON_IsNumber(sample_rate)) {
            server_sample_rate_ = sample_rate->valueint;
        }
        auto frame_duration = cJSON_GetObjectItem(audio_params, "frame_duration");
        if (cJSON_IsNumber(frame_duration)) {
            server_frame_duration_ = frame_duration->valueint;
        }
    }

    xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);
}
