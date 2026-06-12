#include "websocket_protocol.h"
#include "mdns_gateway_discovery.h"
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
                if (!alive->load()) {
                    return;
                }
                protocol->reconnect_timer_armed_.store(false);
                // Re-check intent on the main task. esp_timer_stop() does
                // not cancel work that the timer has already re-posted via
                // Application::Schedule, so a CloseAudioChannel() or
                // destructor that ran *between* timer fire and this lambda
                // executing would otherwise be undone here.
                if (protocol->intentional_close_.load()) {
                    ESP_LOGI(TAG, "Reconnect cancelled (close was intentional)");
                    return;
                }

                auto& app = Application::GetInstance();
                auto state = app.GetDeviceState();
                if (state != kDeviceStateIdle) {
                    ESP_LOGI(TAG, "Reconnect deferred (device state %d != idle); rescheduling", state);
                    protocol->ScheduleReconnect();
                    return;
                }

                ESP_LOGI(TAG, "Reconnecting to websocket server");
                if (!protocol->OpenAudioChannelInternal(false, false)) {
                    // OpenAudioChannelInternal's failure-exit path is now
                    // the single source of reconnect-rescheduling. Calling
                    // ScheduleReconnect() here too would double-advance
                    // reconnect_interval_ms_ (two consecutive
                    // StopReconnectTimer → start_once cycles per failed
                    // retry), driving the backoff to its 60s cap faster
                    // than intended.
                    ESP_LOGW(TAG, "Reconnect attempt failed; OpenAudioChannelInternal will arm next retry");
                }
            });
        },
        .arg = this,
    };
    if (esp_timer_create(&reconnect_timer_args, &reconnect_timer_) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create reconnect timer; auto reconnect will not be available");
        reconnect_timer_ = nullptr;
    }
}

WebsocketProtocol::~WebsocketProtocol() {
    alive_->store(false);
    intentional_close_.store(true);
    transport_connected_.store(false);
    if (current_notify_disconnect_) {
        current_notify_disconnect_->store(false);
    }
    StopReconnectTimer();
    if (reconnect_timer_ != nullptr) {
        esp_timer_delete(reconnect_timer_);
        reconnect_timer_ = nullptr;
    }
    websocket_.reset();
    if (event_group_handle_ != nullptr) {
        vEventGroupDelete(event_group_handle_);
    }
}

bool WebsocketProtocol::Start() {
    // Connect to the configured gateway at boot so MCP control is
    // available before any user interaction (no touch / wake required).
    // arm_audio_channel=false keeps the logical audio-session state
    // separate from the physical WebSocket transport state — see PR #136
    // (CloseAudioChannel keeps the WS alive) and PR #192 (audio_channel_open_
    // is a logical-session flag, see websocket_protocol.h:60-67).
    //
    // If the gateway is unreachable at boot (gateway not started, network
    // not yet stable, token mismatch, etc.), OpenAudioChannelInternal
    // returns false with report_error=false suppressing UI feedback, and
    // arms the reconnect timer via its failure-exit path so the device
    // retries automatically once the gateway becomes reachable.
    // Closes #169.
    return OpenAudioChannelInternal(false, false);
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
    return audio_channel_open_.load() && websocket_ != nullptr && websocket_->IsConnected() && !error_occurred_ && !IsTimeout();
}

bool WebsocketProtocol::IsTransportConnected() const {
    // Returns the cached atomic transport-state flag, which is updated on the
    // WS task (OnDisconnected) and the main task (OpenAudioChannelInternal
    // prologue + success exit, destructor). Read from ESP_TIMER_TASK via
    // Application::CanEnterSleepMode(), so this read must not racily
    // dereference websocket_ while main-task code is calling websocket_.reset().
    // The flag also intentionally ignores Protocol::IsTimeout(), which tracks
    // the audio-session inbound-frame deadline — an idle persistent MCP
    // connection has no inbound audio frames but is still healthy transport.
    return transport_connected_.load();
}

std::string WebsocketProtocol::GetConnectedUrl() const {
    if (!transport_connected_.load()) {
        return "";
    }
    return connected_url_;
}

void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {
    (void)send_goodbye;
    // Keep WebSocket alive — only notify the application that the audio
    // channel is logically closed so it returns to idle state.
    //
    // The original implementation called websocket_.reset() here, which
    // destroyed the WebSocket connection every time the device exited
    // listening/speaking mode. This made it impossible to control the
    // device (LEDs, avatar, head movement) outside of an active audio
    // session, since all MCP tools rely on the same WebSocket.
    //
    // By skipping the teardown and directly invoking the closed callback,
    // the app transitions back to idle while the WebSocket stays connected
    // for continued MCP control.
    audio_channel_open_.store(false);
    // Keep session_id_ across a logical audio-session close; it belongs to the WebSocket connection lifetime.
    ESP_LOGI(TAG, "CloseAudioChannel: keeping WebSocket alive for MCP");
    if (on_audio_channel_closed_ != nullptr) {
        on_audio_channel_closed_();
    }
}

bool WebsocketProtocol::OpenAudioChannel() {
    return OpenAudioChannelInternal(true, true);
}

bool WebsocketProtocol::OpenAudioChannelInternal(bool report_error, bool arm_audio_channel) {
    // Resetting the previous websocket may invoke its OnDisconnected
    // callback synchronously. Disarm the previous socket's flag and
    // mark the teardown as intentional so neither the per-socket lambda
    // nor any deferred reconnect job triggers a spurious reconnect; the
    // new socket below installs a fresh token of its own and clears
    // intentional_close_ once the server hello has been acked.
    audio_channel_open_.store(false);
    transport_connected_.store(false);
    intentional_close_.store(true);
    if (current_notify_disconnect_) {
        current_notify_disconnect_->store(false);
    }
    StopReconnectTimer();
    websocket_.reset();
    // Clear session_id_ at the start of a fresh socket attempt so a
    // malformed or version-skewed server hello cannot leave the previous
    // connection's id active as the tts/listen gate key. CloseAudioChannel
    // intentionally keeps session_id_ alive across a logical audio-session
    // close while the WebSocket stays connected.
    session_id_ = "";
    xEventGroupClearBits(event_group_handle_,
                         WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT |
                         WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED);

    Settings settings("websocket", false);
    // Read the gateway URL from NVS (set via the WiFi config UI's "websocket
    // url" field on first boot, e.g. "ws://<your-gateway-lan-ip>:8765").
    // application.cc forces WebsocketProtocol regardless of the upstream OTA
    // response, and CONFIG_DISABLE_OTA_WEBSOCKET_CONFIG (default y) prevents
    // the upstream OTA server from overwriting the NVS values read below.
    // This firmware always speaks to a stackchan-mcp gateway directly.
    std::string nvs_url = settings.GetString("url");
    std::vector<std::string> gateway_candidates;
    bool force_default_url = false;
#ifdef CONFIG_DEFAULT_WEBSOCKET_URL
#ifdef CONFIG_FORCE_DEFAULT_WEBSOCKET_URL
    // Force mode: Kconfig URL always wins over NVS. Used when NVS contains
    // a stale upstream URL (e.g. wss://api.tenclass.net/...) that no
    // runtime tool can currently overwrite. Only forces when the Kconfig
    // value is non-empty so an unset Kconfig still falls through to NVS.
    if (CONFIG_DEFAULT_WEBSOCKET_URL[0] != '\0') {
        if (!nvs_url.empty() && nvs_url != CONFIG_DEFAULT_WEBSOCKET_URL) {
            ESP_LOGI(TAG,
                     "FORCE: overriding NVS websocket.url with Kconfig: NVS=%s -> %s",
                     nvs_url.c_str(), CONFIG_DEFAULT_WEBSOCKET_URL);
        } else if (nvs_url.empty()) {
            ESP_LOGI(TAG, "FORCE: using Kconfig websocket URL: %s", CONFIG_DEFAULT_WEBSOCKET_URL);
        }
        AddGatewayCandidate(gateway_candidates,
                            CONFIG_DEFAULT_WEBSOCKET_URL,
                            "CONFIG_DEFAULT_WEBSOCKET_URL");
        force_default_url = true;
    }
#endif
#endif
    if (!force_default_url) {
        AddGatewayCandidate(gateway_candidates, nvs_url, "websocket.url");
        if (nvs_url.empty()) {
#ifdef CONFIG_STACKCHAN_MDNS_DISCOVERY
            auto mdns_candidates = DiscoverStackchanGateway(5000);
            if (mdns_candidates.has_value()) {
                for (const auto& mdns_candidate : *mdns_candidates) {
                    AddGatewayCandidate(gateway_candidates,
                                        mdns_candidate.url,
                                        "mDNS _stackchan-mcp._tcp.local.");
                }
            }
#endif
#ifdef CONFIG_DEFAULT_WEBSOCKET_URL
            if (CONFIG_DEFAULT_WEBSOCKET_URL[0] != '\0') {
                ESP_LOGI(TAG,
                         "NVS websocket.url empty; adding build-time default from Kconfig: %s",
                         CONFIG_DEFAULT_WEBSOCKET_URL);
                AddGatewayCandidate(gateway_candidates,
                                    CONFIG_DEFAULT_WEBSOCKET_URL,
                                    "CONFIG_DEFAULT_WEBSOCKET_URL");
            }
#endif
        }
    }

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
        ESP_LOGE(TAG, "WS_URL not configured: no websocket gateway URL candidates available");
        // Do not return early here: fall through to the shared failure exit so
        // intentional_close_ is cleared and ScheduleReconnect() can arm the
        // next retry. Returning here left the latch set from the prologue, so
        // ScheduleReconnect() later refused the retry after a single mDNS
        // 0-result on gateway restart (Issue #61 real-device finding). The
        // loop below naturally performs zero iterations for an empty vector,
        // and the shared exit reports SERVER_NOT_CONNECTED because
        // server_hello_timed_out remains false on this path.
    }

    if (!token.empty() && token.find(" ") == std::string::npos) {
        token = "Bearer " + token;
    }

    bool server_hello_timed_out = false;
    for (size_t i = 0; i < gateway_candidates.size(); ++i) {
        const auto& candidate_url = gateway_candidates[i];

        xEventGroupClearBits(event_group_handle_,
                             WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT |
                             WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED);
        websocket_ = network->CreateWebSocket(1);
        if (websocket_ == nullptr) {
            ESP_LOGE(TAG, "Failed to create websocket");
            continue;
        }
        auto notify_disconnect = std::make_shared<std::atomic<bool>>(false);
        // Per-candidate flag flipped by the OnDisconnected lambda (on the WS
        // task) the moment a live, post-handshake server-side close has
        // already armed a reconnect via ScheduleReconnect(). The main task
        // loads this with acquire semantics immediately after
        // xEventGroupWaitBits() returns the server-hello event, before any
        // success-path mutation. Using a per-socket atomic captured by the
        // lambda gives us a synchronised handshake between the WS task's
        // close path and the main task's resume — unlike reading
        // WebSocket::IsConnected(), whose underlying `connected_` is a
        // plain bool mutated by the WS/TCP callback path with no acquire/
        // release ordering. See #189 round-2 review for the race window.
        auto disconnected_after_hello = std::make_shared<std::atomic<bool>>(false);

        if (!token.empty()) {
            websocket_->SetHeader("Authorization", token.c_str());
        }
        websocket_->SetHeader("Protocol-Version", std::to_string(version_).c_str());
        websocket_->SetHeader("Device-Id", SystemInfo::GetMacAddress().c_str());
        websocket_->SetHeader("Client-Id", Board::GetInstance().GetUuid().c_str());

        websocket_->OnData([this, notify_disconnect, arm_audio_channel](const char* data, size_t len, bool binary) {
            if (binary) {
                // Drop binary frames before parsing when the device is not
                // speaking. This mirrors Application::OnIncomingAudio's
                // downstream gate while avoiding stale-frame parse/allocation.
                if (Application::GetInstance().GetDeviceState() != kDeviceStateSpeaking) {
                    return;
                }
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
                        ParseServerHello(root, notify_disconnect, arm_audio_channel);
                    } else if (strcmp(type->valuestring, "tts") == 0 ||
                               strcmp(type->valuestring, "listen") == 0) {
                        // Drop tts/listen messages whose session_id does not
                        // match the current WebSocket session set by
                        // ParseServerHello, while allowing the gateway's
                        // current-session control messages through.
                        auto session_id_obj = cJSON_GetObjectItem(root, "session_id");
                        const char* incoming_sid = cJSON_IsString(session_id_obj) ? session_id_obj->valuestring : nullptr;
                        bool session_match = incoming_sid != nullptr &&
                                             !session_id_.empty() &&
                                             strcmp(session_id_.c_str(), incoming_sid) == 0;
                        if (!session_match) {
                            ESP_LOGD(TAG, "Dropping %s message (session_id mismatch or missing)", type->valuestring);
                        } else if (on_incoming_json_ != nullptr) {
                            on_incoming_json_(root);
                        }
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

        websocket_->OnDisconnected([this, notify_disconnect, disconnected_after_hello]() {
            audio_channel_open_.store(false);
            transport_connected_.store(false);
            // notify_disconnect carries this socket's reconnect intent.
            // ParseServerHello() arms it (true) once the handshake
            // completes; intentional teardown paths (CloseAudioChannel,
            // OpenAudioChannelInternal, destructor) disarm it (false)
            // before resetting the socket. A false reading here means
            // either the candidate never completed handshake or the
            // close was intentional — neither should reconnect.
            if (!notify_disconnect->load(std::memory_order_acquire)) {
                ESP_LOGI(TAG, "Websocket disconnected (no reconnect: candidate failed or intentional close)");
                return;
            }
            if (on_disconnected_ != nullptr) {
                on_disconnected_();
            }
            ESP_LOGI(TAG, "Websocket disconnected");
            if (on_audio_channel_closed_ != nullptr) {
                on_audio_channel_closed_();
            }
            // Publish "a live disconnect has already armed reconnect" to
            // the main task BEFORE calling ScheduleReconnect(). The release
            // here synchronises with the acquire load in
            // OpenAudioChannelInternal() right after xEventGroupWaitBits(),
            // ensuring the main task observes this store on every code
            // path that would otherwise race in to cancel the just-armed
            // timer via StopReconnectTimer() (#189 round-2 review).
            disconnected_after_hello->store(true, std::memory_order_release);
            ScheduleReconnect();
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

        // Wait for either a successful server hello or an explicit
        // ParseServerHello rejection (#191). Without the FAILED bit, a
        // hello whose transport/session_id is malformed would silently
        // wait out the full 10s timeout per candidate before falling
        // back; with it, we proceed to the next candidate within ~100 ms.
        // The third outcome (neither bit set within 10s — server sent
        // nothing at all) retains the existing timeout-handling path.
        EventBits_t bits = xEventGroupWaitBits(event_group_handle_,
                                               WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT |
                                               WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED,
                                               pdTRUE, pdFALSE, pdMS_TO_TICKS(10000));
        if (bits & WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED) {
            ESP_LOGW(TAG, "Server hello rejected by candidate %d/%d; falling back to next candidate",
                     static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()));
            // Disarm this candidate's reconnect-intent token so the
            // synchronous OnDisconnected fired by websocket_.reset() does
            // not schedule a reconnect for a candidate we are explicitly
            // abandoning. Mirrors the intentional-teardown disarm pattern
            // used in OpenAudioChannelInternal's prologue / destructor /
            // CloseAudioChannel paths.
            notify_disconnect->store(false);
            websocket_.reset();
            continue;
        }
        if (!(bits & WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT)) {
            ESP_LOGE(TAG, "Failed to receive server hello from websocket server candidate %d/%d",
                     static_cast<int>(i + 1), static_cast<int>(gateway_candidates.size()));
            server_hello_timed_out = true;
            websocket_.reset();
            continue;
        }

        // A server-side close arriving between ParseServerHello() setting the
        // wait bit and this main-task resume runs the OnDisconnected lambda on
        // the WS task with notify_disconnect already armed, so the lambda has
        // already called ScheduleReconnect() to arm the reconnect timer.
        // Returning into the success path below would unconditionally call
        // StopReconnectTimer() and cancel that just-armed retry, returning
        // true to the reconnect-timer caller and leaving the transport dead
        // with no retry scheduled (#189).
        //
        // Detect this race via the per-socket disconnected_after_hello flag
        // captured by the OnDisconnected lambda: the lambda stores true with
        // release semantics before ScheduleReconnect(), and the acquire load
        // here establishes happens-before with that store. Unlike
        // WebSocket::IsConnected() — whose `connected_` is a plain bool
        // mutated by the WS/TCP callback path with no synchronisation — this
        // ordering guarantees we observe the close that already armed the
        // reconnect, instead of a stale "still connected" reading
        // (#189 round-2 review). The websocket_ != nullptr check is kept as
        // a defensive guard against unexpected teardown ordering.
        if (websocket_ == nullptr ||
            disconnected_after_hello->load(std::memory_order_acquire)) {
            ESP_LOGW(TAG, "Server-side close raced with server hello; leaving reconnect to the per-socket disconnect handler");
            return false;
        }

        // ParseServerHello() already armed notify_disconnect on the WS
        // task (before setting the wait bit) so a near-simultaneous close
        // is handled by the lambda's reconnect path. Mirror it into the
        // class member here on the main task so CloseAudioChannel /
        // OpenAudioChannelInternal / the destructor can disarm it
        // synchronously when intentionally tearing this socket down.
        current_notify_disconnect_ = notify_disconnect;
        intentional_close_.store(false);
        connected_url_ = candidate_url;
        transport_connected_.store(true);
        reconnect_interval_ms_ = WEBSOCKET_RECONNECT_INITIAL_INTERVAL_MS;
        StopReconnectTimer();

        if (on_connected_ != nullptr) {
            on_connected_();
        }

        if (arm_audio_channel && on_audio_channel_opened_ != nullptr) {
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
    // Clear the intentional_close_ latch (set in the prologue at line ~183)
    // on the failure exit path so ScheduleReconnect can arm a retry.
    // Without this, any subsequent reconnect attempt — including the
    // timer-driven retry the constructor's lambda installs below on
    // recursive failure — would be silently refused because
    // intentional_close_ remained latched at true.
    intentional_close_.store(false);
    ScheduleReconnect();
    return false;
}

void WebsocketProtocol::ScheduleReconnect() {
    if (!alive_->load()) {
        return;
    }
    if (reconnect_timer_ == nullptr) {
        ESP_LOGW(TAG, "Reconnect timer not initialised; cannot schedule reconnect");
        return;
    }
    if (intentional_close_.load()) {
        ESP_LOGI(TAG, "Reconnect not scheduled (intentional close in progress)");
        return;
    }
    bool expected = false;
    if (!reconnect_timer_armed_.compare_exchange_strong(expected, true)) {
        ESP_LOGI(TAG, "Reconnect already scheduled");
        return;
    }

    esp_err_t err = esp_timer_start_once(reconnect_timer_, reconnect_interval_ms_ * 1000);
    if (err != ESP_OK) {
        reconnect_timer_armed_.store(false);
        ESP_LOGW(TAG, "Failed to start reconnect timer (err=%d); reconnect not scheduled", err);
        return;
    }
    ESP_LOGI(TAG, "Schedule websocket reconnect in %d seconds", reconnect_interval_ms_ / 1000);
    reconnect_interval_ms_ = std::min(reconnect_interval_ms_ * 2, WEBSOCKET_RECONNECT_MAX_INTERVAL_MS);
}

void WebsocketProtocol::StopReconnectTimer() {
    reconnect_timer_armed_.store(false);
    if (reconnect_timer_ == nullptr) {
        return;
    }
    esp_err_t err = esp_timer_stop(reconnect_timer_);
    // ESP_ERR_INVALID_STATE just means the timer was not running, which
    // is the common case when StopReconnectTimer() runs from a path
    // where no reconnect is currently armed. Log anything else so a
    // genuinely failed teardown is visible on serial.
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "Failed to stop reconnect timer (err=%d)", err);
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

void WebsocketProtocol::ParseServerHello(const cJSON* root,
                                         const std::shared_ptr<std::atomic<bool>>& notify_disconnect,
                                         bool arm_audio_channel) {
    auto transport = cJSON_GetObjectItem(root, "transport");
    if (transport == nullptr || !cJSON_IsString(transport)) {
        // Surface the rejection to OpenAudioChannelInternal's wait so the
        // candidate loop can fall back to the next URL immediately instead
        // of waiting out the 10s server-hello timeout (#191).
        ESP_LOGE(TAG, "Server hello missing or non-string transport field");
        xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED);
        return;
    }
    if (strcmp(transport->valuestring, "websocket") != 0) {
        ESP_LOGE(TAG, "Unsupported transport: %s", transport->valuestring);
        xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED);
        return;
    }

    auto session_id = cJSON_GetObjectItem(root, "session_id");
    if (!cJSON_IsString(session_id) ||
        session_id->valuestring == nullptr ||
        session_id->valuestring[0] == '\0') {
        // session_id is the gate key for tts/listen messages (#187). A
        // missing or empty session_id at hello time would leave session_id_
        // empty after this PR's OpenAudioChannelInternal clear, causing
        // every gateway-driven tts/listen to mismatch and silently drop.
        // Reject the hello here and signal the failure bit so the candidate
        // loop falls back to the next URL within ~100 ms instead of waiting
        // the full 10s server-hello timeout (#191).
        ESP_LOGE(TAG, "Server hello missing or empty session_id; rejecting candidate");
        xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_FAILED);
        return;
    }
    session_id_ = session_id->valuestring;
    ESP_LOGI(TAG, "Session ID: %s", session_id_.c_str());

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

    // Arm the per-socket reconnect intent BEFORE setting the wait bit so
    // a near-simultaneous server-side close observed by the
    // OnDisconnected lambda still falls into the reconnect path. The
    // release here synchronises with the load() in the OnDisconnected
    // lambda.
    notify_disconnect->store(true, std::memory_order_release);
    // Clear intentional_close_ on the WS task here too, not only after
    // the wait returns on the main task. Without this, if the server
    // closed immediately after sending hello, the OnDisconnected lambda
    // would observe an armed notify_disconnect and call
    // ScheduleReconnect(), but ScheduleReconnect()'s intentional_close_
    // gate (still set by OpenAudioChannelInternal()'s prologue, since
    // the main task has not yet returned from xEventGroupWaitBits) would
    // wrongly suppress the reconnect. Clearing here closes that race;
    // the main task path also clears it for explicitness.
    //
    // The symmetric race — a user-initiated CloseAudioChannel() running
    // between this WS-task clear and the main task mirroring the new
    // notify_disconnect into current_notify_disconnect_ — cannot occur
    // in practice because every CloseAudioChannel() call site in
    // application.cc dispatches on the main task (Application::Run()'s
    // event loop or Schedule() lambdas), and the main task is blocked
    // inside xEventGroupWaitBits for the duration of this window.
    // Reusing this protocol from a context that drives CloseAudioChannel
    // from a separate task would invalidate that assumption and would
    // also need a different mirror strategy (e.g. atomic_shared_ptr).
    // Only arm the audio channel when the user explicitly opened it
    // (OpenAudioChannel → arm_audio_channel=true). Reconnect-driven
    // hellos (arm_audio_channel=false) restore the transport without
    // re-arming audio — otherwise a network blip after
    // CloseAudioChannel() would silently re-open the audio session.
    audio_channel_open_.store(arm_audio_channel);
    intentional_close_.store(false);
    xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);
}
