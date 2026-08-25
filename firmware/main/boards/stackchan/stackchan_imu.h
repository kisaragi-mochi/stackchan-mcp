/*
 * SPDX-FileCopyrightText: 2026 StackChan contributors
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

#include <driver/i2c_master.h>
#include <esp_err.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

struct StackChanImuAxisRaw {
    int16_t x = 0;
    int16_t y = 0;
    int16_t z = 0;
};

struct StackChanImuAxisFloat {
    float x = 0.0f;
    float y = 0.0f;
    float z = 0.0f;
};

struct StackChanImuSnapshot {
    StackChanImuAxisRaw accel_raw;
    StackChanImuAxisRaw gyro_raw;
    StackChanImuAxisRaw mag_raw;
    StackChanImuAxisFloat accel_g;
    StackChanImuAxisFloat gyro_dps;
    StackChanImuAxisFloat mag_ut;
    bool accel_data_ready = false;
    bool gyro_data_ready = false;
    bool mag_data_ready = false;
    bool mag_available = false;
    uint8_t bmi270_i2c_address = 0;
    int64_t sample_time_us = 0;
};

class StackChanImu {
public:
    explicit StackChanImu(i2c_master_bus_handle_t bus);
    ~StackChanImu();

    StackChanImu(const StackChanImu&) = delete;
    StackChanImu& operator=(const StackChanImu&) = delete;

    esp_err_t Initialize();
    esp_err_t Read(StackChanImuSnapshot* snapshot);
    const std::string& last_error() const { return last_error_; }

private:
    esp_err_t InitializeLocked();
    esp_err_t AttachBmi270(uint8_t address);
    esp_err_t InitializeBmi270();
    esp_err_t InitializeBmm150();
    esp_err_t UploadBmi270Config();
    esp_err_t WaitForAuxReady();
    esp_err_t AuxWriteRegister(uint8_t reg, uint8_t value);
    esp_err_t AuxReadRegister(uint8_t reg, uint8_t* value);
    esp_err_t ReadRegisters(uint8_t reg, uint8_t* data, size_t length);
    esp_err_t WriteRegister(uint8_t reg, uint8_t value);
    esp_err_t WriteRegisters(uint8_t reg, const uint8_t* data, size_t length);
    esp_err_t Fail(const char* operation, esp_err_t err);
    void DetachBmi270();

    i2c_master_bus_handle_t bus_ = nullptr;
    i2c_master_dev_handle_t bmi270_ = nullptr;
    SemaphoreHandle_t mutex_ = nullptr;
    bool initialized_ = false;
    bool mag_available_ = false;
    uint8_t bmi270_address_ = 0;
    std::string last_error_;
};
