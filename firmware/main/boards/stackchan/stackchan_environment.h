/*
 * SPDX-FileCopyrightText: 2026 StackChan contributors
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <cstdint>
#include <string>

#include <driver/i2c_master.h>
#include <esp_err.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

struct StackChanEnvironmentSnapshot {
    uint16_t ambient_light_channel0 = 0;
    uint16_t ambient_light_channel1 = 0;
    uint16_t proximity = 0;
    bool ambient_light_data_ready = false;
    bool ambient_light_valid = false;
    bool proximity_data_ready = false;
    bool proximity_saturated = false;
    uint8_t part_id = 0;
    uint8_t manufacturer_id = 0;
    int64_t sample_time_us = 0;
};

// Dedicated LTR-553ALS-WA driver for the CoreS3 system I2C bus. This is
// intentionally separate from generic self.i2c.* tools so those tools cannot
// access the CoreS3's power-management or codec devices on the same bus.
class StackChanEnvironment {
public:
    explicit StackChanEnvironment(i2c_master_bus_handle_t bus);
    ~StackChanEnvironment();

    StackChanEnvironment(const StackChanEnvironment&) = delete;
    StackChanEnvironment& operator=(const StackChanEnvironment&) = delete;

    esp_err_t Initialize();
    esp_err_t Read(StackChanEnvironmentSnapshot* snapshot);
    const std::string& last_error() const { return last_error_; }

private:
    esp_err_t InitializeLocked();
    esp_err_t ReadRegisters(uint8_t reg, uint8_t* data, size_t length);
    esp_err_t WriteRegister(uint8_t reg, uint8_t value);
    esp_err_t Fail(const char* operation, esp_err_t err);
    void Detach();

    i2c_master_bus_handle_t bus_ = nullptr;
    i2c_master_dev_handle_t ltr553_ = nullptr;
    SemaphoreHandle_t mutex_ = nullptr;
    bool initialized_ = false;
    uint8_t part_id_ = 0;
    uint8_t manufacturer_id_ = 0;
    std::string last_error_;
};
