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

enum class StackChanNfcProtocol : uint8_t {
    kNone,
    kIso14443A,
    kNfcF,
};

struct StackChanNfcSnapshot {
    bool tag_present = false;
    bool collision_detected = false;
    StackChanNfcProtocol protocol = StackChanNfcProtocol::kNone;
    uint16_t atqa = 0;
    uint8_t sak = 0;
    uint8_t uid[10] = {};
    size_t uid_length = 0;
    uint8_t idm[8] = {};
    uint8_t pmm[8] = {};
    uint8_t chip_type = 0;
    uint8_t chip_revision = 0;
    int64_t sample_time_us = 0;
};

// Minimal ISO 14443A and NFC-F polling driver for StackChan's body-mounted
// ST25R3916. It detects a single tag and exposes only its identifier metadata.
// It deliberately contains no tag memory reads/writes, authentication, or
// card emulation APIs.
class StackChanNfc {
public:
    explicit StackChanNfc(i2c_master_bus_handle_t bus);
    ~StackChanNfc();

    StackChanNfc(const StackChanNfc&) = delete;
    StackChanNfc& operator=(const StackChanNfc&) = delete;

    esp_err_t Initialize();
    esp_err_t Scan(StackChanNfcSnapshot* snapshot);
    const std::string& last_error() const { return last_error_; }

private:
    esp_err_t InitializeLocked();
    esp_err_t ConfigureIso14443A();
    esp_err_t ConfigureNfcF();
    esp_err_t StartField();
    esp_err_t StopField();
    esp_err_t RequestA(uint16_t* atqa, bool* collision_detected);
    esp_err_t SelectCascadeLevel(uint8_t cascade_level, uint8_t* uid_part,
                                 size_t* uid_part_length, uint8_t* sak,
                                 bool* collision_detected);
    esp_err_t PollNfcF(StackChanNfcSnapshot* snapshot);
    esp_err_t Transmit(const uint8_t* data, size_t length, uint8_t bits,
                       bool append_crc);
    esp_err_t ReadFifo(uint8_t* data, size_t capacity, size_t* actual);
    esp_err_t WaitForMainInterrupt(uint8_t mask, uint32_t timeout_ms,
                                   uint8_t* observed);
    esp_err_t ReadCommand(uint8_t command, uint8_t* data, size_t length);
    esp_err_t WriteCommand(uint8_t command, const uint8_t* data, size_t length);
    esp_err_t ReadRegister(uint8_t reg, uint8_t* value);
    esp_err_t WriteRegister(uint8_t reg, uint8_t value);
    esp_err_t WriteRegister16(uint8_t reg, uint16_t value);
    esp_err_t WriteSpaceBRegister(uint8_t reg, uint8_t value);
    esp_err_t ModifyRegister(uint8_t reg, uint8_t set_mask, uint8_t clear_mask);
    esp_err_t SetNoResponseTimerMs(uint32_t timeout_ms);
    esp_err_t ClearInterrupts();
    esp_err_t Fail(const char* operation, esp_err_t err);
    void Detach();

    i2c_master_bus_handle_t bus_ = nullptr;
    i2c_master_dev_handle_t nfc_ = nullptr;
    SemaphoreHandle_t mutex_ = nullptr;
    bool initialized_ = false;
    uint8_t chip_type_ = 0;
    uint8_t chip_revision_ = 0;
    std::string last_error_;
};
