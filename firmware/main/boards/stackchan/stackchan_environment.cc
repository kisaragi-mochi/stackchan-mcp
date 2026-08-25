/*
 * SPDX-FileCopyrightText: 2026 StackChan contributors
 *
 * SPDX-License-Identifier: MIT
 */

#include "stackchan_environment.h"

#include <cstdio>

#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/task.h>

namespace {

constexpr char kTag[] = "StackChanEnvironment";
constexpr uint8_t kLtr553Address = 0x23;
constexpr uint8_t kLtr553PartIdMask = 0xF0;
constexpr uint8_t kLtr553PartId = 0x90;

constexpr uint8_t kRegAlsControl = 0x80;
constexpr uint8_t kRegPsControl = 0x81;
constexpr uint8_t kRegPsLed = 0x82;
constexpr uint8_t kRegPsPulseCount = 0x83;
constexpr uint8_t kRegPsMeasurementRate = 0x84;
constexpr uint8_t kRegAlsDataChannel1 = 0x88;
constexpr uint8_t kRegStatus = 0x8C;
constexpr uint8_t kRegPsData = 0x8D;

constexpr uint8_t kRegPartId = 0x86;
constexpr uint8_t kRegManufacturerId = 0x87;

// These are the settings in M5Stack's StackChan LTR553 sample: ALS gain 48x,
// 40 kHz PS LED pulses, and a 50 ms PS measurement interval. The values are
// configured before activating either sensing mode, as required by the chip.
constexpr uint8_t kAlsGain48xActive = 0x19;
constexpr uint8_t kPsActiveWithSaturationIndicator = 0x23;
constexpr uint8_t kPsLed40Khz100Percent100mA = 0x3F;
constexpr uint8_t kPsOnePulse = 0x01;
constexpr uint8_t kPsMeasurementRate50Ms = 0x00;

constexpr int kI2cTimeoutMs = 100;

uint16_t ReadUint16Le(const uint8_t* data) {
    return static_cast<uint16_t>(data[0]) |
           (static_cast<uint16_t>(data[1]) << 8);
}

}  // namespace

StackChanEnvironment::StackChanEnvironment(i2c_master_bus_handle_t bus) : bus_(bus) {
    mutex_ = xSemaphoreCreateMutex();
}

StackChanEnvironment::~StackChanEnvironment() {
    Detach();
    if (mutex_ != nullptr) {
        vSemaphoreDelete(mutex_);
    }
}

esp_err_t StackChanEnvironment::Fail(const char* operation, esp_err_t err) {
    char message[128];
    std::snprintf(message, sizeof(message), "%s: %s", operation, esp_err_to_name(err));
    last_error_ = message;
    ESP_LOGE(kTag, "%s", last_error_.c_str());
    return err;
}

void StackChanEnvironment::Detach() {
    if (ltr553_ != nullptr) {
        esp_err_t err = i2c_master_bus_rm_device(ltr553_);
        if (err != ESP_OK) {
            ESP_LOGW(kTag, "Failed to detach LTR-553: %s", esp_err_to_name(err));
        }
        ltr553_ = nullptr;
    }
    initialized_ = false;
    part_id_ = 0;
    manufacturer_id_ = 0;
}

esp_err_t StackChanEnvironment::ReadRegisters(uint8_t reg, uint8_t* data, size_t length) {
    if (ltr553_ == nullptr || data == nullptr || length == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    return i2c_master_transmit_receive(ltr553_, &reg, 1, data, length, kI2cTimeoutMs);
}

esp_err_t StackChanEnvironment::WriteRegister(uint8_t reg, uint8_t value) {
    if (ltr553_ == nullptr) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t data[] = {reg, value};
    return i2c_master_transmit(ltr553_, data, sizeof(data), kI2cTimeoutMs);
}

esp_err_t StackChanEnvironment::InitializeLocked() {
    Detach();

    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = kLtr553Address,
        .scl_speed_hz = 400000,
        .scl_wait_us = 0,
        .flags = {
            .disable_ack_check = false,
        },
    };
    esp_err_t err = i2c_master_bus_add_device(bus_, &config, &ltr553_);
    if (err != ESP_OK) {
        return Fail("attach LTR-553", err);
    }

    uint8_t identity[2] = {};
    err = ReadRegisters(kRegPartId, identity, sizeof(identity));
    if (err != ESP_OK) {
        Detach();
        return Fail("read LTR-553 identity", err);
    }
    if ((identity[0] & kLtr553PartIdMask) != kLtr553PartId) {
        Detach();
        return Fail("validate LTR-553 identity", ESP_ERR_NOT_FOUND);
    }
    part_id_ = identity[0];
    manufacturer_id_ = identity[1];

    // Do not issue a reset: another firmware revision may already have tuned
    // thresholds. This tool owns only the measurement configuration it needs.
    err = WriteRegister(kRegPsLed, kPsLed40Khz100Percent100mA);
    if (err == ESP_OK) err = WriteRegister(kRegPsPulseCount, kPsOnePulse);
    if (err == ESP_OK) err = WriteRegister(kRegPsMeasurementRate, kPsMeasurementRate50Ms);
    if (err == ESP_OK) err = WriteRegister(kRegAlsControl, kAlsGain48xActive);
    if (err == ESP_OK) err = WriteRegister(kRegPsControl, kPsActiveWithSaturationIndicator);
    if (err != ESP_OK) {
        Detach();
        return Fail("configure LTR-553", err);
    }

    // The datasheet specifies a 100 ms initial startup time after activation.
    vTaskDelay(pdMS_TO_TICKS(100));
    initialized_ = true;
    last_error_.clear();
    ESP_LOGI(kTag, "LTR-553 ready (part_id=0x%02X manufacturer_id=0x%02X)",
             part_id_, manufacturer_id_);
    return ESP_OK;
}

esp_err_t StackChanEnvironment::Initialize() {
    if (mutex_ == nullptr || bus_ == nullptr) {
        return Fail("initialize LTR-553", ESP_ERR_INVALID_STATE);
    }
    if (xSemaphoreTake(mutex_, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return Fail("lock LTR-553", ESP_ERR_TIMEOUT);
    }
    esp_err_t err = initialized_ ? ESP_OK : InitializeLocked();
    xSemaphoreGive(mutex_);
    return err;
}

esp_err_t StackChanEnvironment::Read(StackChanEnvironmentSnapshot* snapshot) {
    if (snapshot == nullptr) {
        return Fail("read LTR-553", ESP_ERR_INVALID_ARG);
    }
    if (mutex_ == nullptr || bus_ == nullptr) {
        return Fail("read LTR-553", ESP_ERR_INVALID_STATE);
    }
    if (xSemaphoreTake(mutex_, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return Fail("lock LTR-553", ESP_ERR_TIMEOUT);
    }

    esp_err_t err = initialized_ ? ESP_OK : InitializeLocked();
    uint8_t status = 0;
    uint8_t als_data[4] = {};
    uint8_t ps_data[2] = {};
    if (err == ESP_OK) err = ReadRegisters(kRegStatus, &status, 1);
    // LTR-553 requires the four ALS registers to be read low-to-high as one
    // group; reading 0x8B latches the next coherent sample.
    if (err == ESP_OK) err = ReadRegisters(kRegAlsDataChannel1, als_data, sizeof(als_data));
    if (err == ESP_OK) err = ReadRegisters(kRegPsData, ps_data, sizeof(ps_data));
    if (err != ESP_OK) {
        xSemaphoreGive(mutex_);
        return Fail("read LTR-553 sample", err);
    }

    snapshot->ambient_light_channel1 = ReadUint16Le(als_data);
    snapshot->ambient_light_channel0 = ReadUint16Le(als_data + 2);
    snapshot->proximity = static_cast<uint16_t>(ps_data[0]) |
                          (static_cast<uint16_t>(ps_data[1] & 0x07) << 8);
    snapshot->ambient_light_data_ready = (status & 0x04) != 0;
    snapshot->ambient_light_valid = (status & 0x80) == 0;
    snapshot->proximity_data_ready = (status & 0x01) != 0;
    snapshot->proximity_saturated = (ps_data[1] & 0x80) != 0;
    snapshot->part_id = part_id_;
    snapshot->manufacturer_id = manufacturer_id_;
    snapshot->sample_time_us = esp_timer_get_time();
    last_error_.clear();

    xSemaphoreGive(mutex_);
    return ESP_OK;
}
