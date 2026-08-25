/*
 * SPDX-FileCopyrightText: 2026 StackChan contributors
 *
 * SPDX-License-Identifier: MIT
 */

#include "stackchan_imu.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

#include <bmi270_api.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/task.h>

namespace {

constexpr char kTag[] = "StackChanImu";

constexpr uint8_t kBmi270PrimaryAddress = 0x69;
constexpr uint8_t kBmi270FallbackAddress = 0x68;
constexpr uint8_t kBmi270ChipId = 0x24;
constexpr uint8_t kBmm150Address = 0x10;
constexpr uint8_t kBmm150ChipId = 0x32;

constexpr uint8_t kRegChipId = 0x00;
constexpr uint8_t kRegStatus = 0x03;
constexpr uint8_t kRegAuxData = 0x04;
constexpr uint8_t kRegIntStatus1 = 0x1D;
constexpr uint8_t kRegInternalStatus = 0x21;
constexpr uint8_t kRegAuxDevId = 0x4B;
constexpr uint8_t kRegAuxIfConf = 0x4C;
constexpr uint8_t kRegAuxReadAddr = 0x4D;
constexpr uint8_t kRegAuxWriteAddr = 0x4E;
constexpr uint8_t kRegAuxWriteData = 0x4F;
constexpr uint8_t kRegIntMapData = 0x58;
constexpr uint8_t kRegInitCtrl = 0x59;
constexpr uint8_t kRegInitAddr = 0x5B;
constexpr uint8_t kRegInitData = 0x5E;
constexpr uint8_t kRegIfConf = 0x6B;
constexpr uint8_t kRegPowerConf = 0x7C;
constexpr uint8_t kRegPowerCtrl = 0x7D;
constexpr uint8_t kRegCommand = 0x7E;

constexpr uint8_t kBmi270SoftReset = 0xB6;
constexpr uint8_t kBmm150ChipIdRegister = 0x40;
constexpr uint8_t kBmm150DataRegister = 0x42;
constexpr uint8_t kBmm150PowerRegister = 0x4B;
constexpr uint8_t kBmm150ModeRegister = 0x4C;

constexpr size_t kConfigChunkSize = 32;
constexpr int kI2cTimeoutMs = 100;

int16_t ReadInt16(const uint8_t* data) {
    return static_cast<int16_t>(static_cast<uint16_t>(data[0]) |
                                (static_cast<uint16_t>(data[1]) << 8));
}

}  // namespace

StackChanImu::StackChanImu(i2c_master_bus_handle_t bus) : bus_(bus) {
    mutex_ = xSemaphoreCreateMutex();
}

StackChanImu::~StackChanImu() {
    DetachBmi270();
    if (mutex_ != nullptr) {
        vSemaphoreDelete(mutex_);
    }
}

esp_err_t StackChanImu::Fail(const char* operation, esp_err_t err) {
    char message[128];
    std::snprintf(message, sizeof(message), "%s: %s", operation, esp_err_to_name(err));
    last_error_ = message;
    ESP_LOGE(kTag, "%s", last_error_.c_str());
    return err;
}

void StackChanImu::DetachBmi270() {
    if (bmi270_ != nullptr) {
        esp_err_t err = i2c_master_bus_rm_device(bmi270_);
        if (err != ESP_OK) {
            ESP_LOGW(kTag, "Failed to detach BMI270: %s", esp_err_to_name(err));
        }
        bmi270_ = nullptr;
    }
    initialized_ = false;
    mag_available_ = false;
    bmi270_address_ = 0;
}

esp_err_t StackChanImu::AttachBmi270(uint8_t address) {
    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        // M5Unified uses the CoreS3 internal bus at 100 kHz.  The BMI270
        // configuration stream is unusually sensitive to the host burst
        // timing; keep this device at the board's reference rate while other
        // internal peripherals retain their existing 400 kHz handles.
        .scl_speed_hz = 100000,
        .scl_wait_us = 0,
        .flags = {
            .disable_ack_check = false,
        },
    };
    esp_err_t err = i2c_master_bus_add_device(bus_, &config, &bmi270_);
    if (err != ESP_OK) {
        return err;
    }

    uint8_t chip_id = 0;
    err = ReadRegisters(kRegChipId, &chip_id, 1);
    if (err == ESP_OK && chip_id == kBmi270ChipId) {
        bmi270_address_ = address;
        return ESP_OK;
    }

    i2c_master_bus_rm_device(bmi270_);
    bmi270_ = nullptr;
    return err == ESP_OK ? ESP_ERR_NOT_FOUND : err;
}

esp_err_t StackChanImu::ReadRegisters(uint8_t reg, uint8_t* data, size_t length) {
    if (bmi270_ == nullptr || data == nullptr || length == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = i2c_master_transmit_receive(bmi270_, &reg, 1, data, length, kI2cTimeoutMs);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 read reg 0x%02X (%u bytes) failed: %s",
                 reg, static_cast<unsigned>(length), esp_err_to_name(err));
    }
    return err;
}

esp_err_t StackChanImu::WriteRegister(uint8_t reg, uint8_t value) {
    return WriteRegisters(reg, &value, 1);
}

esp_err_t StackChanImu::WriteRegisters(uint8_t reg, const uint8_t* data, size_t length) {
    if (bmi270_ == nullptr || data == nullptr || length == 0) {
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t buffer[kConfigChunkSize + 1];
    if (length > kConfigChunkSize) {
        return ESP_ERR_INVALID_SIZE;
    }
    buffer[0] = reg;
    std::memcpy(buffer + 1, data, length);
    esp_err_t err = i2c_master_transmit(bmi270_, buffer, length + 1, kI2cTimeoutMs);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 write reg 0x%02X (%u bytes) failed: %s",
                 reg, static_cast<unsigned>(length), esp_err_to_name(err));
    }
    return err;
}

esp_err_t StackChanImu::UploadBmi270Config() {
    // Explicitly enter the configuration-loader state. The soft-reset value
    // is normally zero, but the datasheet requires INIT_CTRL=0 before a new
    // stream and this also makes retries deterministic after a failed load.
    esp_err_t err = WriteRegister(kRegInitCtrl, 0x00);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 config: prepare loader failed: %s", esp_err_to_name(err));
        return err;
    }

    // The BMI270 I2C interface accepts at most a 32-byte burst reliably. For
    // shorter host FIFOs, Bosch requires advancing INIT_ADDR by chunk_size/2
    // words between writes; 0x5B is the low nibble and 0x5C the upper byte.
    for (size_t offset = 0; offset < BMI270_CONFIG_FILE_SIZE; offset += kConfigChunkSize) {
        const uint8_t init_address[] = {
            static_cast<uint8_t>((offset >> 1) & 0x0F),
            static_cast<uint8_t>(offset >> 5),
        };
        err = WriteRegisters(kRegInitAddr, init_address, sizeof(init_address));
        if (err != ESP_OK) {
            ESP_LOGE(kTag, "BMI270 config: set INIT_ADDR at offset %u failed: %s",
                     static_cast<unsigned>(offset), esp_err_to_name(err));
            return err;
        }

        const size_t chunk = std::min(kConfigChunkSize,
                                      static_cast<size_t>(BMI270_CONFIG_FILE_SIZE) - offset);
        err = WriteRegisters(kRegInitData, bmi270_config_file + offset, chunk);
        if (err != ESP_OK) {
            ESP_LOGE(kTag, "BMI270 config: upload offset %u (%u bytes) failed: %s",
                     static_cast<unsigned>(offset), static_cast<unsigned>(chunk),
                     esp_err_to_name(err));
            return err;
        }
    }

    err = WriteRegister(kRegInitCtrl, 0x01);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 config: start feature engine failed: %s", esp_err_to_name(err));
        return err;
    }
    // M5Unified maps the data-ready interrupts before polling INTERNAL_STATUS;
    // keep that ordering in InitializeBmi270().
    return ESP_OK;
}

esp_err_t StackChanImu::WaitForAuxReady() {
    for (int attempt = 0; attempt < 20; ++attempt) {
        uint8_t status = 0;
        esp_err_t err = ReadRegisters(kRegStatus, &status, 1);
        if (err != ESP_OK) {
            return err;
        }
        if ((status & 0x04) == 0) {
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t StackChanImu::AuxWriteRegister(uint8_t reg, uint8_t value) {
    esp_err_t err = WriteRegister(kRegAuxWriteData, value);
    if (err == ESP_OK) {
        err = WriteRegister(kRegAuxWriteAddr, reg);
    }
    if (err == ESP_OK) {
        err = WaitForAuxReady();
    }
    return err;
}

esp_err_t StackChanImu::AuxReadRegister(uint8_t reg, uint8_t* value) {
    esp_err_t err = WriteRegister(kRegAuxIfConf, 0x80);
    if (err == ESP_OK) {
        err = WriteRegister(kRegAuxReadAddr, reg);
    }
    if (err == ESP_OK) {
        err = WaitForAuxReady();
    }
    if (err == ESP_OK) {
        err = ReadRegisters(kRegAuxData, value, 1);
    }
    return err;
}

esp_err_t StackChanImu::InitializeBmm150() {
    esp_err_t err = WriteRegister(kRegIfConf, 0x20);
    if (err == ESP_OK) {
        err = WriteRegister(kRegPowerConf, 0x00);
    }
    if (err == ESP_OK) {
        err = WriteRegister(kRegPowerCtrl, 0x0E);
    }
    if (err == ESP_OK) {
        err = WriteRegister(kRegAuxIfConf, 0x80);
    }
    if (err == ESP_OK) {
        err = WriteRegister(kRegAuxDevId, static_cast<uint8_t>(kBmm150Address << 1));
    }
    if (err != ESP_OK) {
        return err;
    }

    err = AuxWriteRegister(kBmm150PowerRegister, 0x83);
    if (err != ESP_OK) {
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(3));

    // The first AUX read after reset may still contain the previous register's
    // byte. M5Unified intentionally performs this WhoAmI read twice as well.
    uint8_t chip_id = 0;
    err = AuxReadRegister(kBmm150ChipIdRegister, &chip_id);
    if (err == ESP_OK) {
        err = AuxReadRegister(kBmm150ChipIdRegister, &chip_id);
    }
    if (err != ESP_OK) {
        return err;
    }
    if (chip_id != kBmm150ChipId) {
        ESP_LOGW(kTag, "BMM150 not detected through BMI270 AUX (chip_id=0x%02X)", chip_id);
        return ESP_ERR_NOT_FOUND;
    }

    err = AuxWriteRegister(kBmm150ModeRegister, 0x38);
    if (err == ESP_OK) {
        err = WriteRegister(kRegAuxIfConf, 0x4F);
    }
    if (err == ESP_OK) {
        err = WriteRegister(kRegAuxReadAddr, kBmm150DataRegister);
    }
    if (err == ESP_OK) {
        err = WriteRegister(kRegPowerCtrl, 0x0F);
    }
    if (err == ESP_OK) {
        // The first sample is not valid immediately after powering the
        // accelerometer/gyro/auxiliary engine. Give the 100 Hz default ODR
        // one complete sample period before the first self.imu.read call.
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return err;
}

esp_err_t StackChanImu::InitializeBmi270() {
    ESP_LOGI(kTag, "BMI270 init: soft reset");
    esp_err_t err = WriteRegister(kRegCommand, kBmi270SoftReset);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 reset command failed: %s", esp_err_to_name(err));
        return err;
    }

    // BMI270 NACKs register reads while its reset state machine is active.
    // The ESP-IDF new I2C driver reports that NACK as ESP_ERR_INVALID_STATE,
    // so avoid probing PWR_CONF during the reset window.  Sixteen milliseconds
    // matches the retry window in M5Unified's CoreS3 driver and is long enough
    // for the sensor to accept the next command.
    vTaskDelay(pdMS_TO_TICKS(16));

    ESP_LOGI(kTag, "BMI270 init: disable power save");
    err = WriteRegister(kRegPowerConf, 0x00);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 disable power save failed: %s", esp_err_to_name(err));
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(1));

    ESP_LOGI(kTag, "BMI270 init: upload configuration");
    err = UploadBmi270Config();
    if (err != ESP_OK) {
        return err;
    }

    ESP_LOGI(kTag, "BMI270 init: map data interrupts");
    err = WriteRegister(kRegIntMapData, 0xFF);
    if (err != ESP_OK) {
        ESP_LOGE(kTag, "BMI270 map data interrupts failed: %s", esp_err_to_name(err));
        return err;
    }

    uint8_t last_status = 0;
    for (int attempt = 0; attempt < 20; ++attempt) {
        vTaskDelay(pdMS_TO_TICKS(5));
        uint8_t status = 0;
        err = ReadRegisters(kRegInternalStatus, &status, 1);
        last_status = status;
        if (err == ESP_OK && (status & 0x0F) == 0x01) {
            break;
        }
        if (err != ESP_OK) {
            ESP_LOGE(kTag, "BMI270 init: read internal status failed: %s", esp_err_to_name(err));
            return err;
        }
    }
    if ((last_status & 0x0F) != 0x01) {
        ESP_LOGE(kTag, "BMI270 init: feature engine did not become ready (status=0x%02X)",
                 last_status);
        return ESP_ERR_INVALID_RESPONSE;
    }

    ESP_LOGI(kTag, "BMI270 init: initialize BMM150 auxiliary bus");
    err = InitializeBmm150();
    if (err == ESP_OK) {
        mag_available_ = true;
    } else {
        mag_available_ = false;
        ESP_LOGW(kTag, "BMM150 initialization failed; accel/gyro remain available: %s",
                 esp_err_to_name(err));
        err = WriteRegister(kRegPowerCtrl, 0x0E);
    }
    return err;
}

esp_err_t StackChanImu::InitializeLocked() {
    if (initialized_) {
        return ESP_OK;
    }
    if (bus_ == nullptr || mutex_ == nullptr) {
        return Fail("IMU prerequisites unavailable", ESP_ERR_INVALID_STATE);
    }

    DetachBmi270();
    esp_err_t err = AttachBmi270(kBmi270PrimaryAddress);
    if (err != ESP_OK) {
        ESP_LOGW(kTag, "BMI270 not found at 0x%02X; trying 0x%02X",
                 kBmi270PrimaryAddress, kBmi270FallbackAddress);
        err = AttachBmi270(kBmi270FallbackAddress);
    }
    if (err != ESP_OK) {
        return Fail("BMI270 probe failed", err);
    }

    err = InitializeBmi270();
    if (err != ESP_OK) {
        DetachBmi270();
        return Fail("BMI270 initialization failed", err);
    }

    initialized_ = true;
    last_error_.clear();
    ESP_LOGI(kTag, "IMU initialized: BMI270 address=0x%02X, BMM150=%s",
             bmi270_address_, mag_available_ ? "available" : "unavailable");
    return ESP_OK;
}

esp_err_t StackChanImu::Initialize() {
    if (mutex_ == nullptr || xSemaphoreTake(mutex_, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return Fail("IMU initialization mutex timeout", ESP_ERR_TIMEOUT);
    }
    esp_err_t err = InitializeLocked();
    xSemaphoreGive(mutex_);
    return err;
}

esp_err_t StackChanImu::Read(StackChanImuSnapshot* snapshot) {
    if (snapshot == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    if (mutex_ == nullptr || xSemaphoreTake(mutex_, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return Fail("IMU read mutex timeout", ESP_ERR_TIMEOUT);
    }

    esp_err_t err = InitializeLocked();
    if (err != ESP_OK) {
        xSemaphoreGive(mutex_);
        return err;
    }

    // A freshly powered BMI270 can acknowledge register reads before its
    // first accelerometer/gyro frame exists. Poll INT_STATUS_1 rather than
    // returning that all-zero shadow frame to the first caller.
    uint8_t ready_status = 0;
    for (int attempt = 0; attempt < 100; ++attempt) {
        err = ReadRegisters(kRegIntStatus1, &ready_status, 1);
        if (err != ESP_OK) {
            Fail("IMU data-ready status read failed", err);
            xSemaphoreGive(mutex_);
            return err;
        }
        if ((ready_status & 0xC0) == 0xC0) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    if ((ready_status & 0xC0) != 0xC0) {
        err = Fail("IMU sample not ready", ESP_ERR_TIMEOUT);
        xSemaphoreGive(mutex_);
        return err;
    }

    uint8_t data[20] = {};
    err = ReadRegisters(kRegAuxData, data, sizeof(data));
    if (err != ESP_OK) {
        Fail("IMU sample read failed", err);
        xSemaphoreGive(mutex_);
        return err;
    }

    StackChanImuSnapshot result;
    result.mag_raw.x = static_cast<int16_t>(ReadInt16(data) >> 2);
    result.mag_raw.y = static_cast<int16_t>(ReadInt16(data + 2) >> 2);
    result.mag_raw.z = static_cast<int16_t>(ReadInt16(data + 4) & 0xFFFE);
    result.accel_raw.x = ReadInt16(data + 8);
    result.accel_raw.y = ReadInt16(data + 10);
    result.accel_raw.z = ReadInt16(data + 12);
    result.gyro_raw.x = ReadInt16(data + 14);
    result.gyro_raw.y = ReadInt16(data + 16);
    result.gyro_raw.z = ReadInt16(data + 18);

    constexpr float kAccelScale = 8.0f / 32768.0f;
    constexpr float kGyroScale = 2000.0f / 32768.0f;
    constexpr float kMagScale = 10.0f * 4912.0f / 32760.0f;
    result.accel_g = {result.accel_raw.x * kAccelScale,
                      result.accel_raw.y * kAccelScale,
                      result.accel_raw.z * kAccelScale};
    result.gyro_dps = {result.gyro_raw.x * kGyroScale,
                       result.gyro_raw.y * kGyroScale,
                       result.gyro_raw.z * kGyroScale};
    // CoreS3 / StackChan mount the BMM150 with Y and Z inverted relative to
    // the body axes. Match M5Unified's board-specific axis correction.
    result.mag_ut = {result.mag_raw.x * kMagScale,
                     -result.mag_raw.y * kMagScale,
                     -result.mag_raw.z * kMagScale};
    result.mag_available = mag_available_;
    result.accel_data_ready = (ready_status & 0x80) != 0;
    result.gyro_data_ready = (ready_status & 0x40) != 0;
    result.mag_data_ready = mag_available_ && ((ready_status & 0x20) != 0);
    result.bmi270_i2c_address = bmi270_address_;
    result.sample_time_us = esp_timer_get_time();
    *snapshot = result;
    last_error_.clear();
    xSemaphoreGive(mutex_);
    return ESP_OK;
}
