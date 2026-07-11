/*
 * SPDX-FileCopyrightText: 2026 StackChan contributors
 *
 * SPDX-License-Identifier: MIT
 */

#include "stackchan_nfc.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/task.h>

namespace {

constexpr char kTag[] = "StackChanNfc";
constexpr uint8_t kSt25r3916Address = 0x50;
constexpr uint8_t kSt25r3916ChipType = 0x05;

constexpr uint8_t kRegIoConfig1 = 0x00;
constexpr uint8_t kRegIoConfig2 = 0x01;
constexpr uint8_t kRegOperationControl = 0x02;
constexpr uint8_t kRegModeDefinition = 0x03;
constexpr uint8_t kRegBitrateDefinition = 0x04;
constexpr uint8_t kRegIso14443aSettings = 0x05;
constexpr uint8_t kRegAuxiliaryDefinition = 0x0A;
constexpr uint8_t kRegReceiverConfiguration1 = 0x0B;
constexpr uint8_t kRegReceiverConfiguration2 = 0x0C;
constexpr uint8_t kRegReceiverConfiguration3 = 0x0D;
constexpr uint8_t kRegReceiverConfiguration4 = 0x0E;
constexpr uint8_t kRegNoResponseTimer1 = 0x10;
constexpr uint8_t kRegTimerAndEmvControl = 0x12;
constexpr uint8_t kRegMaskMainInterrupt = 0x16;
constexpr uint8_t kRegMainInterrupt = 0x1A;
constexpr uint8_t kRegErrorAndWakeupInterrupt = 0x1C;
constexpr uint8_t kRegPassiveTargetInterrupt = 0x1D;
constexpr uint8_t kRegFifoStatus1 = 0x1E;
constexpr uint8_t kRegNumberOfTransmittedBytes1 = 0x22;
constexpr uint8_t kRegTxDriver = 0x28;
constexpr uint8_t kRegExternalFieldDetectorActivation = 0x2A;
constexpr uint8_t kRegExternalFieldDetectorDeactivation = 0x2B;
constexpr uint8_t kRegAuxiliaryDisplay = 0x31;
constexpr uint8_t kRegIcIdentity = 0x3F;

constexpr uint8_t kCommandSetDefault = 0xC1;
constexpr uint8_t kCommandStopAllActivities = 0xC2;
constexpr uint8_t kCommandTransmitWithCrc = 0xC4;
constexpr uint8_t kCommandTransmitWithoutCrc = 0xC5;
constexpr uint8_t kCommandTransmitReqa = 0xC6;
constexpr uint8_t kCommandInitialFieldOn = 0xC8;
constexpr uint8_t kCommandResetReceiverGain = 0xD5;
constexpr uint8_t kCommandAdjustRegulators = 0xD6;
constexpr uint8_t kCommandClearFifo = 0xDB;
constexpr uint8_t kCommandRegisterSpaceBAccess = 0xFB;
constexpr uint8_t kCommandTestAccess = 0xFC;

constexpr uint8_t kOpReadRegister = 0x40;
constexpr uint8_t kOpLoadFifo = 0x80;
constexpr uint8_t kOpReadFifo = 0x9F;

constexpr uint8_t kOperationEnableOscillator = 0x80;
constexpr uint8_t kOperationEnableReceive = 0x40;
constexpr uint8_t kOperationEnableTransmit = 0x08;
constexpr uint8_t kAuxiliaryNoCrcReceive = 0x80;
constexpr uint8_t kMainIrqReceiveEnd = 0x10;
constexpr uint8_t kMainIrqTransmitEnd = 0x08;
constexpr uint8_t kMainIrqCollision = 0x04;
constexpr uint8_t kAuxiliaryOscillatorReady = 0x10;

constexpr int kI2cTimeoutMs = 100;
constexpr uint32_t kRequestTimeoutMs = 15;
constexpr uint32_t kCascadeTimeoutMs = 20;
constexpr uint32_t kNfcFPollTimeoutMs = 15;
constexpr size_t kMaxFifoDepth = 512;

}  // namespace

StackChanNfc::StackChanNfc(i2c_master_bus_handle_t bus) : bus_(bus) {
    mutex_ = xSemaphoreCreateMutex();
}

StackChanNfc::~StackChanNfc() {
    if (nfc_ != nullptr) {
        (void)StopField();
    }
    Detach();
    if (mutex_ != nullptr) {
        vSemaphoreDelete(mutex_);
    }
}

esp_err_t StackChanNfc::Fail(const char* operation, esp_err_t err) {
    char message[128];
    std::snprintf(message, sizeof(message), "%s: %s", operation, esp_err_to_name(err));
    last_error_ = message;
    ESP_LOGE(kTag, "%s", last_error_.c_str());
    return err;
}

void StackChanNfc::Detach() {
    if (nfc_ != nullptr) {
        esp_err_t err = i2c_master_bus_rm_device(nfc_);
        if (err != ESP_OK) {
            ESP_LOGW(kTag, "Failed to detach ST25R3916: %s", esp_err_to_name(err));
        }
        nfc_ = nullptr;
    }
    initialized_ = false;
    chip_type_ = 0;
    chip_revision_ = 0;
}

esp_err_t StackChanNfc::ReadCommand(uint8_t command, uint8_t* data, size_t length) {
    if (nfc_ == nullptr || data == nullptr || length == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    return i2c_master_transmit_receive(nfc_, &command, 1, data, length, kI2cTimeoutMs);
}

esp_err_t StackChanNfc::WriteCommand(uint8_t command, const uint8_t* data, size_t length) {
    if (nfc_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t buffer[33] = {};
    if (length > sizeof(buffer) - 1 || (length > 0 && data == nullptr)) {
        return ESP_ERR_INVALID_ARG;
    }
    buffer[0] = command;
    if (length > 0) {
        std::memcpy(buffer + 1, data, length);
    }
    return i2c_master_transmit(nfc_, buffer, length + 1, kI2cTimeoutMs);
}

esp_err_t StackChanNfc::ReadRegister(uint8_t reg, uint8_t* value) {
    return ReadCommand(static_cast<uint8_t>(kOpReadRegister | (reg & 0x3F)), value, 1);
}

esp_err_t StackChanNfc::WriteRegister(uint8_t reg, uint8_t value) {
    return WriteCommand(reg & 0x3F, &value, 1);
}

esp_err_t StackChanNfc::WriteRegister16(uint8_t reg, uint16_t value) {
    const uint8_t data[] = {
        static_cast<uint8_t>(value >> 8),
        static_cast<uint8_t>(value),
    };
    return WriteCommand(reg & 0x3F, data, sizeof(data));
}

esp_err_t StackChanNfc::WriteSpaceBRegister(uint8_t reg, uint8_t value) {
    // ST25R3916 requires the Register Space-B access direct command before
    // the normal register-write opcode. This is a single I2C frame:
    // [0xFB, register, value].
    const uint8_t buffer[] = {
        kCommandRegisterSpaceBAccess,
        static_cast<uint8_t>(reg & 0x3F),
        value,
    };
    return i2c_master_transmit(nfc_, buffer, sizeof(buffer), kI2cTimeoutMs);
}

esp_err_t StackChanNfc::ModifyRegister(uint8_t reg, uint8_t set_mask, uint8_t clear_mask) {
    uint8_t value = 0;
    esp_err_t err = ReadRegister(reg, &value);
    if (err != ESP_OK) {
        return err;
    }
    const uint8_t updated = static_cast<uint8_t>((value & ~clear_mask) | set_mask);
    return updated == value ? ESP_OK : WriteRegister(reg, updated);
}

esp_err_t StackChanNfc::SetNoResponseTimerMs(uint32_t timeout_ms) {
    uint8_t timer_control = 0;
    esp_err_t err = ReadRegister(kRegTimerAndEmvControl, &timer_control);
    if (err != ESP_OK) {
        return err;
    }

    const uint32_t step_cycles = (timer_control & 0x01) != 0 ? 4096 : 64;
    const uint64_t numerator = static_cast<uint64_t>(timeout_ms) * 1000u * 13560000u;
    const uint64_t denominator = static_cast<uint64_t>(step_cycles) * 1000000u;
    uint64_t nrt = (numerator + denominator - 1) / denominator;
    if (nrt == 0) {
        nrt = 1;
    }
    if (nrt > 0xFFFFu) {
        nrt = 0xFFFFu;
    }
    return WriteRegister16(kRegNoResponseTimer1, static_cast<uint16_t>(nrt));
}

esp_err_t StackChanNfc::ClearInterrupts() {
    // Reading MAIN clears the error register, so read ERROR first. Reading all
    // sources is also how the official M5 driver acknowledges old IRQs.
    uint8_t discard = 0;
    esp_err_t err = ReadRegister(kRegErrorAndWakeupInterrupt, &discard);
    if (err == ESP_OK) err = ReadRegister(kRegMainInterrupt, &discard);
    if (err == ESP_OK) err = ReadRegister(kRegPassiveTargetInterrupt, &discard);
    return err;
}

esp_err_t StackChanNfc::WaitForMainInterrupt(uint8_t mask, uint32_t timeout_ms,
                                              uint8_t* observed) {
    if (observed == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    *observed = 0;
    const int64_t deadline_us = esp_timer_get_time() + static_cast<int64_t>(timeout_ms) * 1000;
    while (esp_timer_get_time() <= deadline_us) {
        uint8_t flags = 0;
        esp_err_t err = ReadRegister(kRegMainInterrupt, &flags);
        if (err != ESP_OK) {
            return err;
        }
        *observed = static_cast<uint8_t>(*observed | flags);
        if ((*observed & mask) != 0) {
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t StackChanNfc::ReadFifo(uint8_t* data, size_t capacity, size_t* actual) {
    if (data == nullptr || actual == nullptr || capacity == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    *actual = 0;
    uint8_t status[2] = {};
    esp_err_t err = ReadCommand(static_cast<uint8_t>(kOpReadRegister | kRegFifoStatus1),
                                status, sizeof(status));
    if (err != ESP_OK) {
        return err;
    }
    const uint16_t packed = static_cast<uint16_t>(status[0]) << 8 | status[1];
    const size_t count = static_cast<size_t>((packed >> 8) | ((packed & 0x00C0) << 2));
    if (count == 0) {
        return ESP_ERR_NOT_FOUND;
    }
    if (count > kMaxFifoDepth || count > capacity) {
        return ESP_ERR_INVALID_SIZE;
    }
    err = ReadCommand(kOpReadFifo, data, count);
    if (err == ESP_OK) {
        *actual = count;
    }
    return err;
}

esp_err_t StackChanNfc::Transmit(const uint8_t* data, size_t length, uint8_t bits,
                                  bool append_crc) {
    if (data == nullptr || length == 0 || length > kMaxFifoDepth || bits > 7) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ClearInterrupts();
    if (err == ESP_OK) err = WriteCommand(kCommandClearFifo, nullptr, 0);
    if (err == ESP_OK) err = WriteCommand(kOpLoadFifo, data, length);
    if (err == ESP_OK) {
        const uint16_t transmitted = static_cast<uint16_t>((length << 3) | bits);
        err = WriteRegister16(kRegNumberOfTransmittedBytes1, transmitted);
    }
    if (err == ESP_OK) {
        err = WriteCommand(append_crc ? kCommandTransmitWithCrc : kCommandTransmitWithoutCrc,
                           nullptr, 0);
    }
    return err;
}

esp_err_t StackChanNfc::StartField() {
    esp_err_t err = WriteCommand(kCommandInitialFieldOn, nullptr, 0);
    if (err == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(5));
        err = ModifyRegister(kRegOperationControl,
                             kOperationEnableTransmit | kOperationEnableReceive, 0);
    }
    return err;
}

esp_err_t StackChanNfc::StopField() {
    esp_err_t err = WriteCommand(kCommandStopAllActivities, nullptr, 0);
    if (err == ESP_OK) {
        err = ModifyRegister(kRegOperationControl, 0,
                             kOperationEnableTransmit | kOperationEnableReceive);
    }
    return err;
}

esp_err_t StackChanNfc::ConfigureIso14443A() {
    // Mirrors UnitST25R3916::configure_nfc_a().  0x09 is initiator
    // ISO14443A with automatic 106 kbps rate selection; 0x01 alone only
    // carries the automatic-rate bit and leaves the protocol mode unset.
    esp_err_t err = WriteRegister(kRegModeDefinition, 0x09);
    if (err == ESP_OK) err = WriteRegister(kRegBitrateDefinition, 0x00);
    if (err == ESP_OK) err = WriteRegister(kRegIso14443aSettings, 0x00);
    if (err == ESP_OK) err = WriteRegister(kRegIoConfig1, 0x10);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration1, 0x08);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration2, 0x2D);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration3, 0xD8);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration4, 0x22);
    if (err == ESP_OK) err = WriteRegister(kRegMaskMainInterrupt, 0x00);
    if (err == ESP_OK) err = ClearInterrupts();
    if (err == ESP_OK) err = WriteCommand(kCommandResetReceiverGain, nullptr, 0);
    return err;
}

esp_err_t StackChanNfc::ConfigureNfcF() {
    // Mirrors M5UnitUnified's ST25R3916 NFC-F (FeliCa) profile.  This
    // supports SENSF polling only; it does not issue service, read, write,
    // authentication, or emulation commands.
    esp_err_t err = WriteRegister(kRegModeDefinition, 0x1C);  // FeliCa initiator, TR_AM
    if (err == ESP_OK) err = WriteRegister(kRegBitrateDefinition, 0x11);  // 212 kbps TX/RX
    if (err == ESP_OK) err = ModifyRegister(kRegOperationControl, 0x03, 0x00);
    if (err == ESP_OK) err = WriteRegister(kRegIoConfig1, 0x07);
    if (err == ESP_OK) err = WriteRegister(kRegAuxiliaryDefinition, 0x00);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration1, 0x13);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration2, 0x3D);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration3, 0x00);
    if (err == ESP_OK) err = WriteRegister(kRegReceiverConfiguration4, 0x00);
    if (err == ESP_OK) err = WriteSpaceBRegister(0x0C, 0x54);  // correlator 1
    if (err == ESP_OK) err = WriteSpaceBRegister(0x0D, 0x00);  // correlator 2
    if (err == ESP_OK) err = WriteRegister(kRegMaskMainInterrupt, 0x00);
    if (err == ESP_OK) err = ClearInterrupts();
    return err;
}

esp_err_t StackChanNfc::InitializeLocked() {
    Detach();
    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = kSt25r3916Address,
        .scl_speed_hz = 400000,
        .scl_wait_us = 0,
        .flags = {
            .disable_ack_check = false,
        },
    };
    esp_err_t err = i2c_master_bus_add_device(bus_, &config, &nfc_);
    if (err != ESP_OK) {
        return Fail("attach ST25R3916", err);
    }

    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t identity = 0;
    err = ReadRegister(kRegIcIdentity, &identity);
    if (err != ESP_OK) {
        Detach();
        return Fail("read ST25R3916 identity", err);
    }
    chip_type_ = static_cast<uint8_t>((identity >> 3) & 0x1F);
    chip_revision_ = identity & 0x07;
    if (chip_type_ != kSt25r3916ChipType || chip_revision_ == 0) {
        Detach();
        return Fail("validate ST25R3916 identity", ESP_ERR_NOT_FOUND);
    }

    // This power-up and ISO 14443A configuration mirrors the official
    // M5Unit-NFC ST25R3916 driver, reduced to the polling path used here.
    const uint8_t protection_command[] = {0x04, 0x10};
    err = WriteCommand(kCommandStopAllActivities, nullptr, 0);
    if (err == ESP_OK) err = WriteCommand(kCommandSetDefault, nullptr, 0);
    if (err == ESP_OK) err = WriteCommand(kCommandTestAccess, protection_command,
                                          sizeof(protection_command));
    if (err == ESP_OK) err = WriteRegister(kRegIoConfig1, 0x10);  // 400 kHz I2C threshold
    if (err == ESP_OK) err = WriteRegister(kRegIoConfig2, 0xA4);  // 3.3 V, AAT, high IO drive
    if (err == ESP_OK) err = WriteRegister(kRegTxDriver, 0xD0);
    if (err == ESP_OK) err = WriteRegister(kRegExternalFieldDetectorActivation, 0x13);
    if (err == ESP_OK) err = WriteRegister(kRegExternalFieldDetectorDeactivation, 0x02);
    if (err == ESP_OK) err = WriteRegister(kRegMaskMainInterrupt, 0x00);
    if (err == ESP_OK) err = ClearInterrupts();
    if (err == ESP_OK) err = ModifyRegister(kRegOperationControl, kOperationEnableOscillator, 0);
    if (err == ESP_OK) {
        const int64_t deadline_us = esp_timer_get_time() + 50000;
        uint8_t auxiliary = 0;
        do {
            err = ReadRegister(kRegAuxiliaryDisplay, &auxiliary);
            if (err != ESP_OK || (auxiliary & kAuxiliaryOscillatorReady) != 0) {
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(1));
        } while (esp_timer_get_time() <= deadline_us);
        if (err == ESP_OK && (auxiliary & kAuxiliaryOscillatorReady) == 0) {
            err = ESP_ERR_TIMEOUT;
        }
    }
    if (err == ESP_OK) err = WriteCommand(kCommandAdjustRegulators, nullptr, 0);
    if (err == ESP_OK) vTaskDelay(pdMS_TO_TICKS(5));
    if (err == ESP_OK) err = ConfigureIso14443A();
    if (err != ESP_OK) {
        Detach();
        return Fail("configure ST25R3916", err);
    }

    initialized_ = true;
    last_error_.clear();
    ESP_LOGI(kTag, "ST25R3916 ready (type=0x%02X revision=0x%02X)",
             chip_type_, chip_revision_);
    return ESP_OK;
}

esp_err_t StackChanNfc::Initialize() {
    if (mutex_ == nullptr || bus_ == nullptr) {
        return Fail("initialize ST25R3916", ESP_ERR_INVALID_STATE);
    }
    if (xSemaphoreTake(mutex_, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return Fail("lock ST25R3916", ESP_ERR_TIMEOUT);
    }
    const esp_err_t err = initialized_ ? ESP_OK : InitializeLocked();
    xSemaphoreGive(mutex_);
    return err;
}

esp_err_t StackChanNfc::RequestA(uint16_t* atqa, bool* collision_detected) {
    if (atqa == nullptr || collision_detected == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    *atqa = 0;
    *collision_detected = false;
    esp_err_t err = WriteRegister(kRegIso14443aSettings, 0x01);  // anticollision frame
    if (err == ESP_OK) err = ModifyRegister(kRegAuxiliaryDefinition, kAuxiliaryNoCrcReceive, 0);
    if (err == ESP_OK) err = ClearInterrupts();
    if (err == ESP_OK) err = WriteCommand(kCommandClearFifo, nullptr, 0);
    if (err == ESP_OK) err = WriteCommand(kCommandTransmitReqa, nullptr, 0);
    uint8_t irq = 0;
    if (err == ESP_OK) err = WaitForMainInterrupt(kMainIrqReceiveEnd | kMainIrqCollision,
                                                   kRequestTimeoutMs, &irq);
    if (err == ESP_ERR_TIMEOUT) {
        return ESP_ERR_NOT_FOUND;  // no tag is a successful empty scan
    }
    if (err != ESP_OK) {
        return err;
    }
    if ((irq & kMainIrqCollision) != 0) {
        *collision_detected = true;
        return ESP_OK;
    }
    uint8_t response[2] = {};
    size_t actual = 0;
    err = ReadFifo(response, sizeof(response), &actual);
    if (err != ESP_OK || actual != sizeof(response)) {
        return err == ESP_OK ? ESP_ERR_INVALID_RESPONSE : err;
    }
    *atqa = static_cast<uint16_t>(response[1]) << 8 | response[0];
    return ESP_OK;
}

esp_err_t StackChanNfc::PollNfcF(StackChanNfcSnapshot* snapshot) {
    if (snapshot == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }

    // SENSF_REQ: polling command, wildcard system code, no extra request
    // data, one response slot. The ST25R3916 calculates the NFC-F frame
    // length from NUMBER_OF_TRANSMITTED_BYTES, so the command byte is first.
    const uint8_t polling_frame[] = {0x00, 0xFF, 0xFF, 0x00, 0x00};
    esp_err_t err = SetNoResponseTimerMs(kNfcFPollTimeoutMs);
    if (err == ESP_OK) err = Transmit(polling_frame, sizeof(polling_frame), 0, true);

    uint8_t irq = 0;
    if (err == ESP_OK) {
        err = WaitForMainInterrupt(kMainIrqTransmitEnd, kNfcFPollTimeoutMs, &irq);
    }
    if (err == ESP_OK) {
        err = WaitForMainInterrupt(kMainIrqReceiveEnd, kNfcFPollTimeoutMs, &irq);
    }
    if (err == ESP_ERR_TIMEOUT) {
        return ESP_ERR_NOT_FOUND;
    }
    if (err != ESP_OK) {
        return err;
    }

    uint8_t response[18] = {};
    size_t actual = 0;
    err = ReadFifo(response, sizeof(response), &actual);
    if (err != ESP_OK) {
        return err;
    }
    if (actual != sizeof(response) || response[0] < sizeof(response) || response[1] != 0x01) {
        return ESP_ERR_INVALID_RESPONSE;
    }

    std::memcpy(snapshot->idm, response + 2, sizeof(snapshot->idm));
    std::memcpy(snapshot->pmm, response + 10, sizeof(snapshot->pmm));
    snapshot->protocol = StackChanNfcProtocol::kNfcF;
    snapshot->tag_present = true;
    return ESP_OK;
}

esp_err_t StackChanNfc::SelectCascadeLevel(uint8_t cascade_level, uint8_t* uid_part,
                                            size_t* uid_part_length, uint8_t* sak,
                                            bool* collision_detected) {
    if (cascade_level < 1 || cascade_level > 3 || uid_part == nullptr ||
        uid_part_length == nullptr || sak == nullptr || collision_detected == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    *uid_part_length = 0;
    *collision_detected = false;
    const uint8_t select_code = static_cast<uint8_t>(0x91 + cascade_level * 2);
    const uint8_t anticollision[] = {select_code, 0x20};

    esp_err_t err = WriteRegister(kRegIso14443aSettings, 0x01);
    if (err == ESP_OK) err = ModifyRegister(kRegAuxiliaryDefinition, 0, kAuxiliaryNoCrcReceive);
    if (err == ESP_OK) err = Transmit(anticollision, sizeof(anticollision), 0, false);
    uint8_t irq = 0;
    if (err == ESP_OK) err = WaitForMainInterrupt(kMainIrqReceiveEnd | kMainIrqCollision,
                                                   kCascadeTimeoutMs, &irq);
    if (err != ESP_OK) {
        return err;
    }
    if ((irq & kMainIrqCollision) != 0) {
        *collision_detected = true;
        return ESP_OK;
    }

    uint8_t anticollision_response[5] = {};
    size_t actual = 0;
    err = ReadFifo(anticollision_response, sizeof(anticollision_response), &actual);
    if (err != ESP_OK || actual != sizeof(anticollision_response)) {
        return err == ESP_OK ? ESP_ERR_INVALID_RESPONSE : err;
    }
    const uint8_t bcc = static_cast<uint8_t>(anticollision_response[0] ^ anticollision_response[1] ^
                                             anticollision_response[2] ^ anticollision_response[3]);
    if (bcc != anticollision_response[4]) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const bool has_cascade_tag = anticollision_response[0] == 0x88;
    const size_t copied = has_cascade_tag ? 3 : 4;
    std::memcpy(uid_part, anticollision_response + (has_cascade_tag ? 1 : 0), copied);
    *uid_part_length = copied;

    const uint8_t select_frame[] = {
        select_code, 0x70,
        anticollision_response[0], anticollision_response[1], anticollision_response[2],
        anticollision_response[3], anticollision_response[4],
    };
    err = WriteRegister(kRegIso14443aSettings, 0x00);
    if (err == ESP_OK) err = Transmit(select_frame, sizeof(select_frame), 0, true);
    if (err == ESP_OK) err = WaitForMainInterrupt(kMainIrqReceiveEnd, kCascadeTimeoutMs, &irq);
    if (err != ESP_OK) {
        return err;
    }
    uint8_t select_response[3] = {};
    err = ReadFifo(select_response, sizeof(select_response), &actual);
    if (err != ESP_OK || actual < 1) {
        return err == ESP_OK ? ESP_ERR_INVALID_RESPONSE : err;
    }
    *sak = select_response[0];
    return ESP_OK;
}

esp_err_t StackChanNfc::Scan(StackChanNfcSnapshot* snapshot) {
    if (snapshot == nullptr) {
        return Fail("scan NFC", ESP_ERR_INVALID_ARG);
    }
    if (mutex_ == nullptr || bus_ == nullptr) {
        return Fail("scan NFC", ESP_ERR_INVALID_STATE);
    }
    if (xSemaphoreTake(mutex_, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return Fail("lock ST25R3916", ESP_ERR_TIMEOUT);
    }

    *snapshot = {};
    esp_err_t err = initialized_ ? ESP_OK : InitializeLocked();
    bool field_started = false;
    if (err == ESP_OK) err = ConfigureIso14443A();
    if (err == ESP_OK) {
        err = StartField();
        field_started = err == ESP_OK;
    }
    if (err == ESP_OK) {
        err = RequestA(&snapshot->atqa, &snapshot->collision_detected);
        if (err == ESP_ERR_NOT_FOUND) {
            err = ESP_OK;
        }
    }

    if (err == ESP_OK && !snapshot->collision_detected && snapshot->atqa != 0) {
        bool more_cascade_levels = true;
        for (uint8_t level = 1; level <= 3 && more_cascade_levels; ++level) {
            uint8_t part[4] = {};
            size_t part_length = 0;
            bool collision = false;
            err = SelectCascadeLevel(level, part, &part_length, &snapshot->sak, &collision);
            if (err != ESP_OK || collision || snapshot->uid_length + part_length > sizeof(snapshot->uid)) {
                snapshot->collision_detected = collision;
                break;
            }
            std::memcpy(snapshot->uid + snapshot->uid_length, part, part_length);
            snapshot->uid_length += part_length;
            more_cascade_levels = (snapshot->sak & 0x04) != 0;
            if (!more_cascade_levels) {
                snapshot->tag_present = true;
                snapshot->protocol = StackChanNfcProtocol::kIso14443A;
            }
        }
        if (err == ESP_OK && !snapshot->collision_detected && !snapshot->tag_present) {
            err = ESP_ERR_INVALID_RESPONSE;
        }
    }

    // Suica and other FeliCa cards are NFC-F, not ISO 14443A. When no A tag
    // answered, switch profiles and perform one SENSF wildcard poll.
    if (err == ESP_OK && !snapshot->collision_detected && snapshot->atqa == 0) {
        if (field_started) {
            const esp_err_t stop_err = StopField();
            field_started = false;
            if (stop_err != ESP_OK) {
                err = stop_err;
            }
        }
        if (err == ESP_OK) err = ConfigureNfcF();
        if (err == ESP_OK) {
            err = StartField();
            field_started = err == ESP_OK;
        }
        if (err == ESP_OK) {
            err = PollNfcF(snapshot);
            if (err == ESP_ERR_NOT_FOUND) {
                err = ESP_OK;
            }
        }
    }

    if (field_started) {
        const esp_err_t stop_err = StopField();
        if (err == ESP_OK && stop_err != ESP_OK) {
            err = stop_err;
        }
    }
    if (err == ESP_OK) {
        snapshot->chip_type = chip_type_;
        snapshot->chip_revision = chip_revision_;
        snapshot->sample_time_us = esp_timer_get_time();
        last_error_.clear();
    }
    xSemaphoreGive(mutex_);
    return err == ESP_OK ? ESP_OK : Fail("scan NFC", err);
}
