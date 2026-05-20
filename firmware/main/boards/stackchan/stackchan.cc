#include "wifi_board.h"
#include "cores3_audio_codec.h"
#include "display/lcd_display.h"
#include "application.h"
#include "config.h"
#include "power_save_timer.h"
#include "i2c_device.h"
#include "axp2101.h"
#include "mcp_server.h"
// Issue #79: servo driver is selectable at build time via Kconfig.
//   - CONFIG_STACKCHAN_SERVO_SCSCL  (default): GPL-3.0 SCServo_lib
//   - CONFIG_STACKCHAN_SERVO_FEETECH: MIT clean-room driver vendored at
//     firmware/components/feetech_scs/.
// Both drivers share the same begin / WritePos / ReadPos call signatures
// used by this board, but their WritePos success value differs (see
// ServoWritePosOk() below). The rest of stackchan.cc treats both drivers
// uniformly through the ScsBus type alias plus that helper.
#if CONFIG_STACKCHAN_SERVO_FEETECH
#include "feetech_scs.h"
using ScsBus = FeetechScs;
// FeetechScs::WritePos returns 0 on ACK and -1 on bus error.
static inline bool ServoWritePosOk(int r) { return r >= 0; }
#else
#include "SCSCL.h"
using ScsBus = SCSCL;
// SCSCL::WritePos returns 1 on ACK, 0 on ACK timeout, -1 on bus error.
// Treat ACK timeout as failure to keep the original behaviour intact.
static inline bool ServoWritePosOk(int r) { return r > 0; }
#endif
#include "avatar_images.h"

#include <smooth_ui_toolkit.hpp>
#include <esp_log.h>
#include <driver/i2c_master.h>
#include <driver/gpio.h>
#include <driver/uart.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_ili9341.h>
#include <esp_timer.h>
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include "esp_video.h"
#include <cJSON.h>
#include <lvgl.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <memory>
#include <string>
#include <vector>

#define TAG "StackChanBoard"

class Pmic : public Axp2101 {
public:
    // Power Init
    Pmic(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : Axp2101(i2c_bus, addr) {
        uint8_t data = ReadReg(0x90);
        data |= 0b10110100;
        WriteReg(0x90, data);
        WriteReg(0x99, (0b11110 - 5));
        WriteReg(0x97, (0b11110 - 2));
        WriteReg(0x69, 0b00110101);
        WriteReg(0x30, 0b111111);
        WriteReg(0x90, 0xBF);
        WriteReg(0x94, 33 - 5);
        WriteReg(0x95, 33 - 5);
    }

    void SetBrightness(uint8_t brightness) {
        brightness = ((brightness + 641) >> 5);
        WriteReg(0x99, brightness);
    }
};

class CustomBacklight : public Backlight {
public:
    CustomBacklight(Pmic *pmic) : pmic_(pmic) {}

    void SetBrightnessImpl(uint8_t brightness) override {
        pmic_->SetBrightness(target_brightness_);
        brightness_ = target_brightness_;
    }

private:
    Pmic *pmic_;
};

class Aw9523 : public I2cDevice {
public:
    // Exanpd IO Init
    Aw9523(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        WriteReg(0x02, 0b00000111);  // P0
        WriteReg(0x03, 0b10001111);  // P1
        WriteReg(0x04, 0b00011000);  // CONFIG_P0
        WriteReg(0x05, 0b00001100);  // CONFIG_P1
        WriteReg(0x11, 0b00010000);  // GCR P0 port is Push-Pull mode.
        WriteReg(0x12, 0b11111111);  // LEDMODE_P0
        WriteReg(0x13, 0b11111111);  // LEDMODE_P1
    }

    void ResetAw88298() {
        ESP_LOGI(TAG, "Reset AW88298");
        WriteReg(0x02, 0b00000011);
        vTaskDelay(pdMS_TO_TICKS(10));
        WriteReg(0x02, 0b00000111);
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    void ResetIli9342() {
        ESP_LOGI(TAG, "Reset IlI9342");
        WriteReg(0x03, 0b10000001);
        vTaskDelay(pdMS_TO_TICKS(20));
        WriteReg(0x03, 0b10000011);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
};

class Ft6336 : public I2cDevice {
public:
    struct TouchPoint_t {
        int num = 0;
        int x = -1;
        int y = -1;
    };
    
    Ft6336(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        uint8_t chip_id = ReadReg(0xA3);
        ESP_LOGI(TAG, "Get chip ID: 0x%02X", chip_id);
        read_buffer_ = new uint8_t[6];
    }

    ~Ft6336() {
        delete[] read_buffer_;
    }

    void UpdateTouchPoint() {
        ReadRegs(0x02, read_buffer_, 6);
        tp_.num = read_buffer_[0] & 0x0F;
        tp_.x = ((read_buffer_[1] & 0x0F) << 8) | read_buffer_[2];
        tp_.y = ((read_buffer_[3] & 0x0F) << 8) | read_buffer_[4];
    }

    inline const TouchPoint_t& GetTouchPoint() {
        return tp_;
    }

private:
    uint8_t* read_buffer_ = nullptr;
    TouchPoint_t tp_;
};

// Minimal PY32 IO Expander driver (servo power switch on pin 0 / VM EN).
// Ported from M5Stack-BSP PY32IOExpander.cpp.
//
// IMPORTANT: this class deliberately does NOT inherit from I2cDevice.
// The base I2cDevice registers every device at scl_speed_hz = 400 kHz, but
// the M5 reference implementation (`PY32IOExpander_Class`) defaults to
// 100 kHz, and 400 kHz appears to leave PY32 in a half-finished slave
// state — `i2c_master_probe` returns ACK but the very next
// `i2c_master_transmit_receive` for REG_VERSION times out (0x103) every
// time. We register our own i2c_master device handle at 100 kHz to match
// the M5 default. Other peripherals on the bus (Si12T at 0x68, AXP2101,
// AW9523, FT6336) keep using the 400 kHz path through I2cDevice.
//
// Reliability notes:
//  - Each I2C op transparently retries up to I2C_INNER_RETRIES on transient
//    errors, with a short vTaskDelay between attempts.
//  - All bit-level write helpers propagate success as bool so the caller
//    can decide whether the GPIO actually got configured.
//  - Begin() can optionally return the version byte so the caller can log
//    which attempt finally talked to the chip.
class Py32IoExpander {
public:
    static constexpr uint8_t  DEFAULT_ADDR = 0x6F;
    static constexpr uint32_t I2C_FREQ_HZ  = 100000;  // 100 kHz (M5 default)
    static constexpr uint8_t  REG_GPIO_O_L_PUBLIC = 0x05;  // exposed for verify

    Py32IoExpander(i2c_master_bus_handle_t i2c_bus, uint8_t addr = DEFAULT_ADDR) {
        i2c_device_config_t cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address  = addr,
            .scl_speed_hz    = I2C_FREQ_HZ,
            .scl_wait_us     = 0,
            .flags           = { .disable_ack_check = 0 },
        };
        ESP_ERROR_CHECK(i2c_master_bus_add_device(i2c_bus, &cfg, &i2c_device_));
    }

    // Probe the chip. On success, returns true and (if non-null) writes the
    // version byte to out_version. Internal reads use SafeReadReg, which
    // already retries on transient I2C errors — so the chip is genuinely
    // unreachable / not yet ready when this returns false.
    bool Begin(uint8_t* out_version = nullptr) {
        uint8_t version = 0;
        if (!SafeReadReg(REG_VERSION, &version)) {
            return false;
        }
        if (version == 0x00 || version == 0xFF) {
            return false;
        }
        if (out_version != nullptr) {
            *out_version = version;
        }
        return true;
    }

    // direction: false=input, true=output. Accepts pin 0..15 (PY32 has 14
    // GPIOs; the WS2812 data line is on pin 13, in the high byte).
    bool SetDirection(uint8_t pin, bool output) {
        return WriteBitWideSafe(REG_GPIO_M_L, REG_GPIO_M_H, pin, output);
    }

    // mode: false=pull down, true=pull up. Accepts pin 0..15.
    bool SetPullMode(uint8_t pin, bool up) {
        if (up) {
            bool a = WriteBitWideSafe(REG_GPIO_PD_L, REG_GPIO_PD_H, pin, false);
            bool b = WriteBitWideSafe(REG_GPIO_PU_L, REG_GPIO_PU_H, pin, true);
            return a && b;
        } else {
            bool a = WriteBitWideSafe(REG_GPIO_PU_L, REG_GPIO_PU_H, pin, false);
            bool b = WriteBitWideSafe(REG_GPIO_PD_L, REG_GPIO_PD_H, pin, true);
            return a && b;
        }
    }

    bool DigitalWrite(uint8_t pin, bool level) {
        return WriteBitSafe(REG_GPIO_O_L, pin, level);
    }

    // Read back the current output low-byte register (pins 0..7) for
    // verification after DigitalWrite. Returns false if the read failed.
    bool ReadOutputLow(uint8_t* out) {
        return SafeReadReg(REG_GPIO_O_L, out);
    }

    // Drive mode for any pin (0..15). false=push-pull, true=open-drain.
    // The WS2812 data line on pin 13 must be push-pull.
    bool SetDriveMode(uint8_t pin, bool open_drain) {
        return WriteBitWideSafe(REG_GPIO_DRV_L, REG_GPIO_DRV_H, pin, open_drain);
    }

    // ---- LED (WS2812 driven by the PY32 itself, data line on pin 13) ----
    // REG_LED_CFG packs both the LED count (bits 0-5, max 32) and the latch
    // trigger (bit 6). Writing the count clears bit 6, which is fine because
    // it's a self-clearing strobe. RefreshLeds() does read-modify-write so
    // the count is preserved when we latch.
    bool SetLedCount(uint8_t count) {
        if (count > 32) count = 32;
        return SafeWriteReg(REG_LED_CFG, count & 0x3F);
    }

    // Set one LED to RGB888. RGB888 → RGB565 packing matches the M5 BSP:
    // ((r&0xF8)<<8) | ((g&0xFC)<<3) | (b>>3), little-endian on the wire.
    // Does NOT latch — call RefreshLeds() once after a batch of updates.
    bool SetLedColor(uint8_t index, uint8_t r, uint8_t g, uint8_t b) {
        if (index >= 32) return false;
        uint16_t v = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
        uint8_t buf[3] = { (uint8_t)(REG_LED_RAM_START + index * 2),
                           (uint8_t)(v & 0xFF),
                           (uint8_t)((v >> 8) & 0xFF) };
        return SafeWriteRaw(buf, sizeof(buf));
    }

    // Burst-write up to N LED RGB565 pairs starting at index 0. data is
    // packed { lo0, hi0, lo1, hi1, ... } and len is the byte count
    // (=2*num_leds, max 64). Single I2C transaction — much faster than
    // calling SetLedColor in a loop. Does NOT latch.
    bool SetLedData(const uint8_t* data, size_t len) {
        if (data == nullptr || len == 0) return false;
        if (len > 64) len = 64;
        uint8_t buf[1 + 64];
        buf[0] = REG_LED_RAM_START;
        for (size_t i = 0; i < len; i++) buf[1 + i] = data[i];
        return SafeWriteRaw(buf, 1 + len);
    }

    // Latch the LED RAM out to the WS2812 strip. Read-modify-write so the
    // current count (bits 0-5) is preserved alongside the latch bit (bit 6).
    bool RefreshLeds() {
        uint8_t cfg = 0;
        if (!SafeReadReg(REG_LED_CFG, &cfg)) return false;
        return SafeWriteReg(REG_LED_CFG, (uint8_t)(cfg | (1u << 6)));
    }

private:
    // Owned device handle (NOT inherited from I2cDevice — see class comment).
    // Registered at 100 kHz in the constructor so this transport runs slower
    // than the rest of the bus.
    i2c_master_dev_handle_t i2c_device_ = nullptr;

    static constexpr uint8_t REG_VERSION  = 0x02;
    static constexpr uint8_t REG_GPIO_M_L = 0x03;  // Direction (mode) low byte
    static constexpr uint8_t REG_GPIO_M_H = 0x04;  // Direction (mode) high byte
    static constexpr uint8_t REG_GPIO_O_L = 0x05;  // Output low byte
    static constexpr uint8_t REG_GPIO_O_H = 0x06;  // Output high byte
    static constexpr uint8_t REG_GPIO_PU_L = 0x09; // Pull-up low byte
    static constexpr uint8_t REG_GPIO_PU_H = 0x0A; // Pull-up high byte
    static constexpr uint8_t REG_GPIO_PD_L = 0x0B; // Pull-down low byte
    static constexpr uint8_t REG_GPIO_PD_H = 0x0C; // Pull-down high byte
    static constexpr uint8_t REG_GPIO_DRV_L = 0x13; // Drive mode low byte
    static constexpr uint8_t REG_GPIO_DRV_H = 0x14; // Drive mode high byte
    static constexpr uint8_t REG_LED_CFG       = 0x24;  // count[5:0] + latch[6]
    static constexpr uint8_t REG_LED_RAM_START = 0x30;  // 2 bytes per LED, RGB565 LE

    // I2C op transient-retry parameters. Total budget per failed op is
    // (I2C_INNER_RETRIES - 1) * I2C_RETRY_DELAY_MS, i.e. ~30 ms here.
    static constexpr int I2C_INNER_RETRIES  = 3;
    static constexpr int I2C_RETRY_DELAY_MS = 15;

    // Safe I2C read — retries up to I2C_INNER_RETRIES on transient errors,
    // logs WARN only on the final failure to keep the log readable.
    bool SafeReadReg(uint8_t reg, uint8_t* out) {
        esp_err_t err = ESP_FAIL;
        for (int i = 0; i < I2C_INNER_RETRIES; i++) {
            err = i2c_master_transmit_receive(i2c_device_, &reg, 1, out, 1, 100);
            if (err == ESP_OK) {
                return true;
            }
            if (i + 1 < I2C_INNER_RETRIES) {
                vTaskDelay(pdMS_TO_TICKS(I2C_RETRY_DELAY_MS));
            }
        }
        ESP_LOGW("Py32IoExpander", "I2C read reg 0x%02X failed after %d tries: 0x%X",
                 reg, I2C_INNER_RETRIES, err);
        return false;
    }

    // Safe I2C write — same retry semantics as SafeReadReg.
    bool SafeWriteReg(uint8_t reg, uint8_t value) {
        uint8_t buffer[2] = {reg, value};
        esp_err_t err = ESP_FAIL;
        for (int i = 0; i < I2C_INNER_RETRIES; i++) {
            err = i2c_master_transmit(i2c_device_, buffer, 2, 100);
            if (err == ESP_OK) {
                return true;
            }
            if (i + 1 < I2C_INNER_RETRIES) {
                vTaskDelay(pdMS_TO_TICKS(I2C_RETRY_DELAY_MS));
            }
        }
        ESP_LOGW("Py32IoExpander", "I2C write reg 0x%02X failed after %d tries: 0x%X",
                 reg, I2C_INNER_RETRIES, err);
        return false;
    }

    // Read-Modify-Write a single bit using safe I2C. Pin 0..7 only.
    // Returns false if either the read or the write step ultimately failed.
    bool WriteBitSafe(uint8_t reg, uint8_t pin, bool value) {
        if (pin >= 8) {
            return false;
        }
        uint8_t v = 0;
        if (!SafeReadReg(reg, &v)) {
            return false;
        }
        if (value) {
            v |= (uint8_t)(1u << pin);
        } else {
            v &= (uint8_t)~(1u << pin);
        }
        return SafeWriteReg(reg, v);
    }

    // 16-bit RMW: pin 0..7 -> reg_l, pin 8..15 -> reg_h. Used for any pin
    // beyond pin 7 (LED data line is on pin 13, so all the LED setup goes
    // through this path).
    bool WriteBitWideSafe(uint8_t reg_l, uint8_t reg_h, uint8_t pin, bool value) {
        if (pin >= 16) return false;
        uint8_t reg = (pin < 8) ? reg_l : reg_h;
        uint8_t bit = (uint8_t)(pin & 0x07);
        uint8_t v = 0;
        if (!SafeReadReg(reg, &v)) return false;
        if (value) v |= (uint8_t)(1u << bit);
        else       v &= (uint8_t)~(1u << bit);
        return SafeWriteReg(reg, v);
    }

    // Burst write: ship a pre-built {reg, ...payload} buffer in a single
    // i2c_master_transmit. Used by the LED RAM writes which would otherwise
    // require dozens of individual register writes. Same retry semantics
    // as SafeWriteReg.
    bool SafeWriteRaw(const uint8_t* buf, size_t len) {
        esp_err_t err = ESP_FAIL;
        for (int i = 0; i < I2C_INNER_RETRIES; i++) {
            err = i2c_master_transmit(i2c_device_, buf, len, 100);
            if (err == ESP_OK) {
                return true;
            }
            if (i + 1 < I2C_INNER_RETRIES) {
                vTaskDelay(pdMS_TO_TICKS(I2C_RETRY_DELAY_MS));
            }
        }
        ESP_LOGW("Py32IoExpander", "I2C raw write (len=%u) failed after %d tries: 0x%X",
                 (unsigned)len, I2C_INNER_RETRIES, err);
        return false;
    }
};

// Minimal Si12T driver (12-channel capacitive touch sensor, TSM12-compatible).
// Used for the StackChan head-stroke / head-tap detection. Only the read path
// (Output1 register, channels 1-4) is needed for Phase 7. We expose just the
// first three channels through ReadTouchState() because the StackChan head
// has 3 conductive zones wired to TS1..TS3.
//
// Datasheet excerpt:
//   - I2C 7-bit address: 0xD0 >> 1 == 0x68 when ID_SEL pin is tied to GND.
//     This matches the address probed at boot ("0x68").
//   - Reset value of CTRL (0x09) is 0b00000111 (SLEEP=1). We must clear SLEEP
//     to enter normal sensing mode: CTRL = 0b00000011.
//   - Output1 (0x10) packs four channels into one byte (2 bits per channel):
//       bit[1:0] = OUT1, bit[3:2] = OUT2, bit[5:4] = OUT3, bit[7:6] = OUT4
//       00 = no output, 01 = low, 10 = medium, 11 = high.
//   - There is no dedicated chip-id register, so Begin() validates the device
//     via successful I2C ACK on the CTRL read + non-0xFF Output1 read.
class Si12T : public I2cDevice {
public:
    static constexpr uint8_t DEFAULT_ADDR = 0x68;  // ID_SEL = GND

    struct TouchState {
        bool zone[3];          // CH1, CH2, CH3 — true if any output level set
        uint8_t output1_raw;   // raw Output1 register byte (0x10)
        bool ok;               // false if the I2C read failed
    };

    Si12T(i2c_master_bus_handle_t i2c_bus, uint8_t addr = DEFAULT_ADDR)
        : I2cDevice(i2c_bus, addr) {}

    // Probe the chip and bring it out of sleep. Returns true on success.
    bool Begin() {
        uint8_t ctrl = 0;
        if (!SafeReadReg(REG_CTRL, &ctrl)) {
            return false;
        }
        // CTRL bit1 = SLEEP. Clear it; bit1:0 must hold 1 per datasheet
        // ("CTRL Bit1, Bit0 = 1 1" reset value), so write 0b00000011.
        if (!SafeWriteReg(REG_CTRL, 0x03)) {
            return false;
        }
        // Verify the device actually responds on the output register.
        // 0xFF would indicate an open bus / no device.
        uint8_t out1 = 0;
        if (!SafeReadReg(REG_OUTPUT1, &out1)) {
            return false;
        }
        if (out1 == 0xFF) {
            ESP_LOGW("Si12T", "Output1 read 0xFF (likely no device)");
            return false;
        }
        ESP_LOGI("Si12T", "init OK: ctrl=0x%02X out1=0x%02X (sleep cleared)", ctrl, out1);
        return true;
    }

    // Sample channels CH1..CH3 from Output1 (0x10). Single-shot read; the
    // caller is expected to debounce / interpret duration externally.
    TouchState ReadTouchState() {
        TouchState s = {};
        s.ok = false;
        if (!SafeReadReg(REG_OUTPUT1, &s.output1_raw)) {
            return s;
        }
        s.ok = true;
        // Each channel uses 2 bits; nonzero = touched at some level.
        s.zone[0] = ((s.output1_raw >> 0) & 0x3) != 0;  // CH1
        s.zone[1] = ((s.output1_raw >> 2) & 0x3) != 0;  // CH2
        s.zone[2] = ((s.output1_raw >> 4) & 0x3) != 0;  // CH3
        return s;
    }

private:
    static constexpr uint8_t REG_CTRL    = 0x09;  // CTRL, SLEEP bit etc.
    static constexpr uint8_t REG_OUTPUT1 = 0x10;  // CH1..CH4 packed (2bpp)

    bool SafeReadReg(uint8_t reg, uint8_t* out) {
        esp_err_t err = i2c_master_transmit_receive(i2c_device_, &reg, 1, out, 1, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Si12T", "I2C read reg 0x%02X failed: 0x%X", reg, err);
            return false;
        }
        return true;
    }

    bool SafeWriteReg(uint8_t reg, uint8_t value) {
        uint8_t buffer[2] = {reg, value};
        esp_err_t err = i2c_master_transmit(i2c_device_, buffer, 2, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Si12T", "I2C write reg 0x%02X failed: 0x%X", reg, err);
            return false;
        }
        return true;
    }
};

class StackChanBoard : public WifiBoard {
private:
    // Internal I2C bus (shared by AXP2101 / AW9523 / FT6336 / PY32 / Si12T /
    // audio codec / IMU). Direct on-board ICs only; not exposed through
    // self.i2c.* MCP tools.
    i2c_master_bus_handle_t i2c_bus_;
    // External I2C bus dedicated to Grove Port A. Exposed through self.i2c.*
    // MCP tools so the gateway can drive attached M5Stack Unit modules.
    i2c_master_bus_handle_t port_a_i2c_bus_;
    Pmic* pmic_;
    Aw9523* aw9523_;
    Ft6336* ft6336_;
    LcdDisplay* display_;
    EspVideo* camera_;
    esp_timer_handle_t touchpad_timer_;
    PowerSaveTimer* power_save_timer_;
    ScsBus scs_bus_;
    std::unique_ptr<Py32IoExpander> io_expander_;

    // Avatar overlay state. avatar_img_ is created lazily on the active LVGL
    // screen because the screen tree (container_, emoji_label_, ...) is built
    // by Application::Start() -> Display::SetupUI(), which runs after this
    // board's constructor completes. avatar_init_timer_ retries every 500 ms
    // until the screen is ready, then stops itself.
    lv_obj_t* avatar_img_ = nullptr;
    esp_timer_handle_t avatar_init_timer_ = nullptr;
    std::string current_avatar_face_ = "idle";

    // Phase 2: blinking + lip-sync overlay state.
    // Blink works as a four-step state machine driven by blink_step_timer_:
    //   FACE -> EYES_HALF -> EYES_CLOSED -> EYES_HALF -> FACE (restore last face)
    // Each step is BLINK_STEP_MS apart. While a blink is in progress, further
    // schedule events are dropped (we run to completion before re-arming).
    // blink_schedule_timer_ fires every 3-6s (re-armed each cycle) and triggers
    // a new blink only if blink_enabled_ and no other blink is in flight.
    enum class BlinkState : uint8_t {
        IDLE = 0,
        EYES_HALF_DOWN,
        EYES_CLOSED,
        EYES_HALF_UP,
    };
    static constexpr int BLINK_STEP_MS = 100;
    static constexpr int BLINK_MIN_GAP_MS = 3000;
    static constexpr int BLINK_MAX_GAP_MS = 6000;
    esp_timer_handle_t blink_schedule_timer_ = nullptr;
    esp_timer_handle_t blink_step_timer_ = nullptr;
    BlinkState blink_state_ = BlinkState::IDLE;
    bool blink_enabled_ = false;
    // Captures blink_enabled_ at the moment SetAvatarOff() runs, so that a
    // later set_avatar(<other face>) can restore the previous blink state.
    // Only meaningful while current_avatar_face_ == "off".
    bool blink_enabled_before_off_ = false;

    // Phase 4 audio (Issue #76): state-driven TTS lip-sync animation.
    // Driven by the gateway's tts.start / tts.stop notifications (see
    // Application::OnIncomingJson) via Board::OnTtsStart / OnTtsStop;
    // cycles the mouth through closed -> half -> open -> half on a fixed
    // TTS_LIPSYNC_STEP_MS cadence until stopped. Autonomous blink is paused
    // while active (same Phase 2 trade-off as the mouth-sequence task: a
    // blink ending would otherwise restore the full-face image and overwrite
    // the mouth overlay). The user's most recent blink intent is read from
    // blink_desired_ at stop so a set_blink call issued during playback is
    // honoured.
    enum class TtsLipSyncShape : uint8_t {
        CLOSED = 0,
        HALF_RISING,   // closed -> open transition
        OPEN,
        HALF_FALLING,  // open -> closed transition
    };
    static constexpr int TTS_LIPSYNC_STEP_MS = 150;
    esp_timer_handle_t tts_lipsync_timer_ = nullptr;
    std::atomic<bool> tts_lipsync_active_{false};
    TtsLipSyncShape tts_lipsync_shape_ = TtsLipSyncShape::CLOSED;

    // Phase 7: Si12T head-touch sensing.
    // Polling every TOUCH_POLL_MS samples Output1 (CH1..CH3 -> 3 head zones).
    // Edge detection on the OR of the three zones produces TAP / STROKE
    // gestures based on hold duration:
    //   duration <  TAP_MAX_MS (400 ms)  -> TAP    -> face=surprised
    //   duration >= STROKE_MIN_MS (600 ms) -> STROKE -> face=embarrassed + servo wobble
    //   400 <= duration < 600 ms         -> treated as TAP (greyzone)
    // Reactions auto-revert to "idle" after REACTION_HOLD_MS (3 s). A
    // post-reaction COOLDOWN_MS lock-out prevents one head-pat from firing
    // a chain of events.
    enum class TouchEvent : uint8_t {
        IDLE = 0,
        TAP,
        STROKE,
    };
    static constexpr int TOUCH_POLL_MS    = 100;  // 100 Hz polling
    static constexpr int TAP_MAX_MS       = 400;
    static constexpr int STROKE_MIN_MS    = 400;  // was 600; lowered because
                                                  // finger-glide between zones
                                                  // and Si12T auto-recalibration
                                                  // inject brief "all-false"
                                                  // gaps that cut a real stroke
                                                  // short of 600 ms.
    static constexpr int REACTION_HOLD_MS = 3000;
    static constexpr int COOLDOWN_MS      = 800;  // post-reaction noise gate
    // With 2-sample debounce this gives ~200 ms confirm latency, fast enough
    // to catch a quick "pon" (~200 ms press) while still rejecting single-
    // sample jitter. Was 200 ms polling -> 400 ms confirm, which silently
    // dropped most short taps.
    static constexpr int SERVO_WOBBLE_STEP_MS = 350;  // was 200; SCS0009 needs
                                                       // ~125 ms to physically
                                                       // travel ±20°, plus the
                                                       // ACK round-trip + IFG.
                                                       // Tighter steps caused
                                                       // bus hangs.
    static constexpr int SERVO_WOBBLE_AMPLITUDE_DEG = 20;

    std::unique_ptr<Si12T> si12t_;
    bool si12t_ok_ = false;
    esp_timer_handle_t touch_poll_timer_ = nullptr;
    esp_timer_handle_t touch_revert_timer_ = nullptr;

    // Touch detection state (single-thread access from the touch_poll_timer_
    // callback, which runs on the ESP_TIMER_TASK).
    bool touch_pressed_prev_ = false;          // last sample (debounced)
    bool touch_pressed_pending_ = false;       // candidate awaiting confirm
    int  touch_pending_count_ = 0;             // consecutive samples matching
    uint64_t touch_press_start_us_ = 0;        // when pressed_prev_ went true
    uint64_t cooldown_until_us_ = 0;           // ignore press until this ts

    // Last reported event for MCP get_touch_state.
    TouchEvent last_event_ = TouchEvent::IDLE;
    uint64_t   last_event_us_ = 0;
    bool       last_zone_snapshot_[3] = {false, false, false};
    uint8_t    last_output1_raw_ = 0;

    // Servo wobble sub-state. Keeps the previously-set angles untouched
    // before/after the wobble so that an external set_head_angles call is
    // not silently overwritten beyond the wobble window.
    std::atomic<int> servo_wobble_step_{0};       // 0..3 sequence index
    std::atomic<bool> servo_wobble_active_{false};

    // Shared motion state. This stays on the board singleton because boot-init
    // ReadPos restore / re-sync phases seed the same state before and after the
    // concrete MotionDriver is selected. Drivers only borrow these references.
    struct AxisMotion {
        int target_deg = 0;
        int start_deg = 0;
        int current_deg = 0;
        uint32_t move_start_ms = 0;
        // For the delegated driver: time the WritePos for this request
        // was successfully ACK'd by the servo (i.e. when the physical
        // motion actually began on the SCS0009's internal clock). May
        // be later than move_start_ms when the dispatch is delayed by
        // Tick wake or retry rounds. ApplyReadMoveResult's stuck-high
        // timeout is measured from dispatch_start_ms, not from staging,
        // so that degraded-bus latency does not cause premature force-
        // clear while the servo is genuinely still mid-motion. 0 means
        // "not yet dispatched"; ApplyReadMoveResult skips the
        // stuck-high check until dispatch_start_ms is populated by
        // FinishDispatch's write_ok branch. HostInterpolation path
        // does not consult this field.
        uint32_t dispatch_start_ms = 0;
        uint32_t move_duration_ms = 0;
        bool moving = false;
        // Monotonic counter incremented by ServoDelegatedMotionDriver::
        // StartMove on each dispatch stage. Used by Tick() to detect
        // when a newer StartMove has raced in between the snapshot at
        // the top of Tick() and the post-WritePos / post-ReadMove
        // commit (motion_mutex_ is dropped while the bus operation
        // runs). esp_timer_get_time()/1000 has 1 ms resolution and is
        // not unique enough on its own — two StartMove calls within
        // the same millisecond would collide on move_start_ms. The
        // HostInterpolation path does not consult this field; the
        // monotonic counter is owned by the delegated driver.
        uint64_t request_token = 0;
        // Set by the delegated driver when ApplyReadMoveResult's
        // 5-consecutive-failure force-clear fires: WritePos ACK
        // confirmed the servo received the command, but ReadMove
        // polling never observed completion. current_deg holds the
        // last optimistic commit (the requested target), but the
        // physical head may be mid-trajectory or at the wrong angle.
        // The next StartMove on this axis treats position_unknown as
        // a "force re-dispatch" signal (no-op skip is suppressed),
        // so a same-target retry surfaces the failure rather than
        // hiding it behind a stale-but-equal current_deg. The
        // HostInterpolation path does not consult this field.
        bool position_unknown = false;
    };
    class MotionDriver;
    // TODO: motion_mutex_/scs_bus_mutex_/servo_task_handle_ have no destroy path; board is singleton via DECLARE_BOARD.
    AxisMotion yaw_motion_;
    AxisMotion pitch_motion_;
    SemaphoreHandle_t motion_mutex_ = nullptr;     // protects AxisMotion fields
    SemaphoreHandle_t scs_bus_mutex_ = nullptr;    // serializes UART access (WritePos/ReadPos)
    std::unique_ptr<MotionDriver> motion_driver_;
    TaskHandle_t servo_task_handle_ = nullptr;
    uint32_t last_motion_end_ms_ = 0;              // ServoTask-private
    bool last_motion_end_valid_ = false;           // ServoTask-private
    std::atomic<bool> idle_timer_reset_pending_{false};
    enum class TorqueState : uint8_t {
        kEngaged = 0,
        kPartial = 1,
        kReleased = 2,
        kReleasing = 3,
    };
    std::atomic<TorqueState> torque_state_{TorqueState::kEngaged};
    std::atomic<uint32_t> torque_release_epoch_{0};
    // Per-axis commanded torque state is protected by scs_bus_mutex_;
    // torque_state_ publishes the derived cross-task summary.
    bool yaw_torque_enabled_ = true;               // protected by scs_bus_mutex_
    bool pitch_torque_enabled_ = true;             // protected by scs_bus_mutex_
    std::atomic<bool> boot_init_done_{false};
#if CONFIG_STACKCHAN_AUTO_TORQUE_RELEASE_ENABLED
    std::atomic<bool> auto_release_enabled_{true};
#else
    std::atomic<bool> auto_release_enabled_{false};
#endif
    static constexpr uint32_t MOTION_TICK_MS = 20;
    static constexpr uint32_t MOTION_DEFAULT_DURATION_MS = 600;
    static constexpr uint32_t MOTION_PER_WRITE_TIME_MS = 30;
    static constexpr uint32_t MOTION_POLL_INTERVAL_MS = 50;
    static constexpr uint32_t AUTO_TORQUE_RELEASE_MIN_MS = 500;
    static constexpr uint32_t AUTO_TORQUE_RELEASE_MAX_MS = 600000;
    static constexpr int kMaxReengageRetries = 3;
    static constexpr int kMaxManualReengageRetries = 3;
#ifdef CONFIG_STACKCHAN_AUTO_TORQUE_RELEASE_MS
    static constexpr uint32_t AUTO_TORQUE_RELEASE_DEFAULT_MS =
        CONFIG_STACKCHAN_AUTO_TORQUE_RELEASE_MS;
#else
    static constexpr uint32_t AUTO_TORQUE_RELEASE_DEFAULT_MS = 5000;
#endif
    std::atomic<uint32_t> auto_release_timeout_ms_{
        AUTO_TORQUE_RELEASE_DEFAULT_MS};

    // Issue #80 / #98: pitch is guarded by two complementary tiers.
    //
    // Tier 1 — Hard clamp [SAFE_PITCH_MIN, SAFE_PITCH_MAX]:
    //   The absolute mechanical safety net. Its only job is to prevent
    //   physical damage to the servo / gear / chassis. Values are silently
    //   clamped to this range at every servo-write boundary and an
    //   ESP_LOGW is emitted when clamping occurs.
    //   - Lower bound 0°: the mechanical end-stop on the M5Stack CoreS3 +
    //     SCS0009 hardware sits very close to pitch=-1° (validated on a
    //     real unit, PR #81). Driving below 0° presses the servo gear into
    //     the physical stopper and produces an audible click.
    //   - Upper bound (SAFE_PITCH_MAX): chosen 1° inside the validated
    //     mechanical upper limit. The M5Stack-documented servo features
    //     advertise "90-degree movement on the vertical axis", so the
    //     mechanical upper end-stop is expected near 90°; the precise
    //     value is established by real-device sweep (Issue #98 validation).
    //
    // Tier 2 — Recommended operating range [RECOMMENDED_PITCH_MIN,
    //          RECOMMENDED_PITCH_MAX]:
    //   The M5Stack-documented sweet spot for long-term servo reliability
    //   (https://docs.m5stack.com/en/StackChan, "Motion Angle Notice":
    //   "The movement angle of the StackChan Y-axis servo (vertical
    //   direction) is recommended to be controlled within 5 ~ 85°.
    //   Operating at extreme angles may cause servo stall and permanent
    //   damage.").
    //   Values inside the hard clamp but outside this range are accepted
    //   (they are not hardware-damaging on a single call), and an
    //   ESP_LOGI is emitted so callers / agents can notice the deviation
    //   without blocking the motion.
    //
    // Defense-in-depth: the hard clamp is enforced at every servo-write
    // boundary —
    //   1. PitchDegToPos() clamps its input (covers motion-task
    //      interpolation and any future caller that bypasses the MCP
    //      layer).
    //   2. The start-up restore from ReadPos clamps the recovered angle
    //      so a device booting with the head physically pushed past the
    //      safe range does not carry that out-of-range starting angle
    //      into motion interpolation.
    //   3. The set_head_angles MCP handler additionally clamps the
    //      request target so the original out-of-range value is logged.
    static constexpr int SAFE_PITCH_MIN = 0;
    static constexpr int SAFE_PITCH_MAX = 88;  // Issue #98: validated on real hardware
                                                // (M5Stack CoreS3 + SCS0009 ×2). On-device
                                                // sweep observed clean motion at pitch=85
                                                // and pitch=88 reached without end-stop, but
                                                // pitch=89 exhibited an audible sub-stall
                                                // ("ji-ji-" gear strain sound). Mirrors PR #81
                                                // lower-bound rationale: stay 1° inside the
                                                // observed servo-strain boundary.
    static constexpr int RECOMMENDED_PITCH_MIN = 5;   // M5Stack official docs
    static constexpr int RECOMMENDED_PITCH_MAX = 85;  // M5Stack official docs

    // Issue #115: boot-time initialization target. Fall-safe neutral pose
    // well clear of both mechanical end-stops, in the centre of the
    // M5Stack-recommended 5..85° pitch range. Design follows the
    // goHome() pattern in m5stack/StackChan
    // (apps/app_setup/workers/servo.cpp:144) and the 1-second
    // positioning timing established in mongonta0716/stackchan-arduino
    // attachServos().
    //
    // Speed policy history (#121 Problem 2 -> #141 follow-ups):
    // - #121 Problem 2 originally raised BOOT_INIT_MOVE_MS from 1000 ms
    //   (the historical default that produced a startling "ブルンっ" boot
    //   motion) to 4000 ms (~11°/s on a 45° climb).
    // - Real-device verification then showed even 11°/s reads as
    //   perceptibly fast for the first boot-time servo motion, so the
    //   Phase 0 climb is pinned to an angular-speed cap of
    //   BOOT_INIT_TARGET_DEG_PER_SEC=15 deg/s. The WriteHeadAngles call
    //   sizes its duration from the actual yaw / pitch deltas so this
    //   cap holds on every axis. On the PMIC OFF/ON path Phase 0 stays
    //   a no-op of effect (the #138 safe-fallback seed makes start_deg
    //   == target_deg), so the BOOT_INIT_MOVE_MS budget simply elapses
    //   without WritePos movement.
    // The 100 ms post-settle vTaskDelay in InitializeServo() is
    // unchanged. The separate "unintended downward drop on power-on"
    // (#121 Problem 1) is addressed by the snap-suppress hold in
    // InitializeServo() Phase 1a (PR #137) and the ReadPos retry +
    // safe-fallback seed in this file (#138).
    //
    // Issue #138: promoted from local block scope inside
    // InitializeServo() to class-level static constexpr so the
    // safe-fallback branch in Phase 2 can seed
    // pitch_motion_.current_deg with BOOT_INIT_PITCH_DEG when the
    // pre-init ReadPos retries all fail. Without that seed, the
    // boot-init `WriteHeadAngles(0, 45, 4000)` interpolation would
    // start from the struct-default `current_deg=0` (== pos=620 at
    // deg=0, the lower mechanical end-stop) and walk WritePos calls
    // upward through end-stop-adjacent positions before reaching the
    // target, risking servo bus degradation if the SCS0009 wakes up
    // mid-sequence.
    static constexpr int BOOT_INIT_YAW_DEG = 0;
    static constexpr int BOOT_INIT_PITCH_DEG = 45;
    // BOOT_INIT_MOVE_MS=3000: minimum duration of the Phase 0 climb.
    // Used as a floor so the boot-init `WriteHeadAngles(0, 45, X)`
    // always elapses at least this long — required on the PMIC OFF/ON
    // path where the #138 safe-fallback seed makes Phase 0 a no-op of
    // effect, and the BOOT_INIT_MOVE_MS budget instead serves to span
    // the SCS0009 wake-up latency window so the post-init ReadPos
    // (Phase 0') lands well past it. The actual Phase 0 duration is
    // computed at call time from the current_deg → BOOT_INIT_*
    // deltas at BOOT_INIT_TARGET_DEG_PER_SEC=15 deg/s, then floored at this
    // constant; e.g. on the ESP32-only reset path with a yaw-90° prior
    // set-point, Phase 0 needs 6000 ms to honour the speed cap while a
    // yaw-0 prior is rounded up to this 3000 ms floor. This is the
    // no-stutter Smooth lower bound established under #121 Problem 2
    // (Issue #121 / PR #125 history: 1000 -> 4000 was a partial step
    // toward this; on-device feedback under #141 verification confirmed
    // 15 deg/s is the speed at which the ServoTask MOTION_TICK_MS=20 ms
    // interpolation stops being perceptible as individual position
    // jumps without sliding into a startling regime). Boot-time budget
    // is intentionally not optimised: operator safety and avoiding
    // mechanical stress take precedence over shaving seconds off the
    // initialization duration.
    //
    // On the PMIC OFF/ON path Phase 0 stays a no-op of effect
    // because the #138 safe-fallback seeds current_deg to
    // BOOT_INIT_PITCH_DEG, making start_deg == target_deg; the
    // BOOT_INIT_MOVE_MS budget then simply elapses without WritePos
    // movement.
    static constexpr uint32_t BOOT_INIT_MOVE_MS = 3000;
    // Single-source Phase 0 speed cap. 15 deg/s is the no-stutter Smooth
    // lower bound (#121 Problem 2 + #141 verification).
    static constexpr int BOOT_INIT_TARGET_DEG_PER_SEC = 15;

    static int YawDegToPos(int deg) {
        int pos = 460 + deg * 16 / 5;
        if (pos < 0) pos = 0;
        if (pos > 1000) pos = 1000;
        return pos;
    }

    static int PitchDegToPos(int deg) {
        // Issue #80: defense-in-depth — clamp at the servo-write boundary so
        // motion-task interpolation and any other future caller cannot
        // bypass the input-layer clamp.
        if (deg < SAFE_PITCH_MIN) deg = SAFE_PITCH_MIN;
        if (deg > SAFE_PITCH_MAX) deg = SAFE_PITCH_MAX;
        int pos = 620 + deg * 16 / 5;
        if (pos < 0) pos = 0;
        if (pos > 1000) pos = 1000;
        return pos;
    }

    static uint16_t clamp_u16(uint32_t v) {
        if (v > std::numeric_limits<uint16_t>::max()) {
            return std::numeric_limits<uint16_t>::max();
        }
        return static_cast<uint16_t>(v);
    }

    // Map the StartMove duration contract to spring options that approximate
    // the requested timing. This is the stackchan-mcp side of
    // m5stack/StackChan's map_speed_to_spring_options(speed): shorter
    // duration -> higher stiffness/damping, longer duration -> lower
    // stiffness/damping, with critical damping for no overshoot.
    static smooth_ui_toolkit::SpringOptions_t MapDurationToSpringOptions(
        uint32_t duration_ms) {
        if (duration_ms == 0) {
            duration_ms = 1;
        }

        float speed_f = 500.0f *
            (static_cast<float>(MOTION_DEFAULT_DURATION_MS) /
             static_cast<float>(duration_ms));
        if (speed_f < 1.0f) speed_f = 1.0f;
        if (speed_f > 1000.0f) speed_f = 1000.0f;
        int speed = static_cast<int>(speed_f);

        constexpr float kMin = 10.0f;
        constexpr float kMax = 650.0f;
        constexpr float kMass = 1.0f;
        float normalized_speed = static_cast<float>(speed) / 1000.0f;
        float stiffness =
            kMin + (normalized_speed * normalized_speed) * (kMax - kMin);
        float damping = 2.0f * std::sqrt(kMass * stiffness);

        smooth_ui_toolkit::SpringOptions_t options;
        options.stiffness = stiffness;
        options.damping = damping;
        options.mass = kMass;
        options.velocity = 0.0f;
        options.restDelta = speed > 800 ? 0.5f : 0.1f;
        options.restSpeed = speed > 800 ? 0.5f : 0.1f;
        options.duration = 0.0f;
        options.bounce = 0.0f;
        options.visualDuration = 0.0f;
        return options;
    }

    enum class ReleaseReason : uint8_t {
        kManual = 0,
        kAutoIdle,
        kReengagement,
    };

    static const char* ReleaseReasonName(ReleaseReason reason) {
        switch (reason) {
            case ReleaseReason::kManual:
                return "manual";
            case ReleaseReason::kAutoIdle:
                return "auto_idle";
            case ReleaseReason::kReengagement:
                return "reengagement";
        }
        return "unknown";
    }

    // Caller must hold scs_bus_mutex_.
    void PublishTorqueState() {
        TorqueState state;
        if (yaw_torque_enabled_ && pitch_torque_enabled_) {
            state = TorqueState::kEngaged;
        } else if (!yaw_torque_enabled_ && !pitch_torque_enabled_) {
            state = TorqueState::kReleased;
        } else {
            state = TorqueState::kPartial;
        }
        TorqueState old_state =
            torque_state_.load(std::memory_order_acquire);
        if (state == TorqueState::kEngaged &&
            old_state != TorqueState::kEngaged) {
            // Reset the ServoTask-owned idle window even when OFF->ON
            // happens between ServoTask ticks.
            idle_timer_reset_pending_.store(true,
                                            std::memory_order_release);
        }
        torque_state_.store(state, std::memory_order_release);
    }

    // Marks a fully-OFF transition while the bus write is still pending.
    // Returns this call's release epoch so auto-idle rollback can detect
    // another release publisher that interleaved after it.
    uint32_t MarkReleasing() {
        uint32_t epoch =
            torque_release_epoch_.fetch_add(1, std::memory_order_acq_rel) +
            1;
        torque_state_.store(TorqueState::kReleasing,
                            std::memory_order_release);
        return epoch;
    }

    // Block until torque_state_ leaves kReleasing or the elapsed-time
    // budget expires. Caller must NOT hold motion_mutex_ or scs_bus_mutex_.
    // Uses esp_timer_get_time() so the budget is honored at real time
    // regardless of CONFIG_FREERTOS_HZ.
    //
    // Returns true if the state is no longer kReleasing (proceed safely),
    // false if the wait budget was exhausted while still kReleasing
    // (caller decides how to handle: either skip with ESP_LOGW or defer to
    // its own bounded retry).
    bool WaitForKReleasingToClear() {
        constexpr uint32_t kMaxKReleasingWaitMs = 200;
        constexpr uint32_t kKReleasingPollIntervalMs = 5;
        const TickType_t kDelayTicks =
            std::max<TickType_t>(1,
                                 pdMS_TO_TICKS(kKReleasingPollIntervalMs));
        const uint32_t start_us =
            static_cast<uint32_t>(esp_timer_get_time());
        auto state = torque_state_.load(std::memory_order_acquire);
        while (state == TorqueState::kReleasing) {
            const uint32_t elapsed_ms =
                (static_cast<uint32_t>(esp_timer_get_time()) - start_us) /
                1000;
            if (elapsed_ms >= kMaxKReleasingWaitMs) {
                return false;
            }
            vTaskDelay(kDelayTicks);
            state = torque_state_.load(std::memory_order_acquire);
        }
        return true;
    }

    struct ServoTorqueResult {
        int yaw_bus_return = -1;
        int pitch_bus_return = -1;
        bool yaw_ok = false;
        bool pitch_ok = false;
        bool short_circuited = false;
    };

    class MotionDriver {
    public:
        virtual ~MotionDriver() = default;

        // Non-blocking. Both axes are dispatched within a single call.
        // WriteHeadAngles holds motion_mutex_ while calling this method; Tick()
        // and getters take the mutex internally for their own state access.
        virtual void StartMove(float yaw_deg, float pitch_deg,
                               uint32_t duration_ms,
                               bool prefer_linear = false) = 0;

        // Last-known committed angle for each axis.
        virtual float GetYawDeg() const = 0;
        virtual float GetPitchDeg() const = 0;

        // True iff at least one axis is currently in motion.
        virtual bool IsMoving() const = 0;

        // Called from ServoTask body at a driver-dependent cadence.
        virtual void Tick() = 0;

        // Optional hooks for drivers that need setup or shutdown.
        virtual bool Initialize() { return true; }
        virtual void Shutdown() {}

        // Invalidate the freshness token for one axis. Used by board-
        // level code that mutates AxisMotion fields directly outside
        // StartMove (currently InitializeServo's Phase 0' post-init
        // ReadPos re-sync, and the set_servo_torque MCP tool's
        // disable path). Caller must hold motion_mutex_.
        //
        // HostInterpolationMotionDriver: bumps the per-axis
        // request_token, defeating any post-bus freshness check from a
        // Tick() snapshot taken before the external mutation.
        //
        // ServoDelegatedMotionDriver: bumps the per-axis request_token
        // AND clears the corresponding AxisServo's per-axis private
        // cancellation state (pending_dispatch_, dispatch_failures_,
        // readmove_failures_), atomically with the caller's motion_mutex_
        // hold.
        //
        // Drivers without a token-based freshness guard treat this as
        // a no-op (default implementation). Argument is SERVO_YAW_ID
        // or SERVO_PITCH_ID; unknown values are ignored.
        virtual void InvalidateAxisToken(int /*axis_id*/) {}
    };

    class HostInterpolationMotionDriver final : public MotionDriver {
    public:
        HostInterpolationMotionDriver(ScsBus& scs_bus,
                                      SemaphoreHandle_t& scs_bus_mutex,
                                      SemaphoreHandle_t& motion_mutex,
                                      AxisMotion& yaw_motion,
                                      AxisMotion& pitch_motion)
            : scs_bus_(scs_bus),
              scs_bus_mutex_(scs_bus_mutex),
              motion_mutex_(motion_mutex),
              yaw_motion_(yaw_motion),
              pitch_motion_(pitch_motion),
              next_request_token_(0) {
            yaw_anim_.teleport(static_cast<float>(yaw_motion_.current_deg));
            pitch_anim_.teleport(static_cast<float>(pitch_motion_.current_deg));
        }

        void StartMove(float yaw_deg, float pitch_deg,
                       uint32_t duration_ms,
                       bool prefer_linear) override {
            uint32_t now_ms = static_cast<uint32_t>(esp_timer_get_time() / 1000);
            int yaw = static_cast<int>(yaw_deg);
            int pitch = static_cast<int>(pitch_deg);

            yaw_motion_.request_token = ++next_request_token_;
            yaw_motion_.target_deg = yaw;
            yaw_motion_.start_deg = yaw_motion_.current_deg;
            yaw_motion_.move_start_ms = now_ms;
            yaw_motion_.move_duration_ms = duration_ms;
            yaw_motion_.moving = (yaw_motion_.target_deg != yaw_motion_.current_deg);
            yaw_linear_mode_ = prefer_linear;

            pitch_motion_.request_token = ++next_request_token_;
            pitch_motion_.target_deg = pitch;
            pitch_motion_.start_deg = pitch_motion_.current_deg;
            pitch_motion_.move_start_ms = now_ms;
            pitch_motion_.move_duration_ms = duration_ms;
            pitch_motion_.moving = (pitch_motion_.target_deg != pitch_motion_.current_deg);
            pitch_linear_mode_ = prefer_linear;
            if (prefer_linear) {
                yaw_anim_.teleport(static_cast<float>(yaw_motion_.current_deg));
                yaw_snap_on_rest_ = false;
                pitch_anim_.teleport(static_cast<float>(pitch_motion_.current_deg));
                pitch_snap_on_rest_ = false;
                return;
            }

            smooth_ui_toolkit::SpringOptions_t spring_options =
                MapDurationToSpringOptions(duration_ms);
            StartAxisSpring(yaw_anim_, yaw_snap_on_rest_,
                            yaw_motion_.current_deg, yaw,
                            yaw_motion_.moving, spring_options);
            StartAxisSpring(pitch_anim_, pitch_snap_on_rest_,
                            pitch_motion_.current_deg, pitch,
                            pitch_motion_.moving, spring_options);
        }

        float GetYawDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int yaw = yaw_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(yaw);
        }

        float GetPitchDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int pitch = pitch_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(pitch);
        }

        bool IsMoving() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            bool moving = yaw_motion_.moving || pitch_motion_.moving;
            xSemaphoreGive(motion_mutex_);
            return moving;
        }

        void Tick() override {
            constexpr TickType_t kInterFrameGap = pdMS_TO_TICKS(10);

            vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));

            AxisMotion yaw_local;
            AxisMotion pitch_local;
            int new_yaw_current;
            bool new_yaw_moving;
            int new_pitch_current;
            bool new_pitch_moving;
            bool yaw_linear_mode;
            bool pitch_linear_mode;
            uint64_t now_us = static_cast<uint64_t>(esp_timer_get_time());
            float dt_s;
            // Spring mode follows real elapsed time so bus ACK latency or
            // mutex contention does not stretch animation time indefinitely.
            // Clamp deep preemption to avoid a single large lurch.
            if (last_tick_us_ == 0) {
                dt_s = static_cast<float>(MOTION_TICK_MS) / 1000.0f;
            } else {
                dt_s = static_cast<float>(now_us - last_tick_us_) / 1000000.0f;
                if (dt_s > 0.1f) {
                    dt_s = 0.1f;
                }
            }
            last_tick_us_ = now_us;
            uint32_t now_ms = static_cast<uint32_t>(now_us / 1000);

            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            yaw_local = yaw_motion_;
            pitch_local = pitch_motion_;
            yaw_linear_mode = yaw_linear_mode_;
            pitch_linear_mode = pitch_linear_mode_;
            if (!yaw_local.moving && !pitch_local.moving) {
                xSemaphoreGive(motion_mutex_);
                return;
            }
            new_yaw_current = yaw_local.current_deg;
            new_yaw_moving = yaw_local.moving;
            if (yaw_linear_mode) {
                AdvanceAxisLinear(yaw_local, now_ms,
                                  new_yaw_current, new_yaw_moving);
            } else {
                AdvanceAxisSpring(yaw_local, yaw_anim_, yaw_snap_on_rest_,
                                  dt_s, new_yaw_current, new_yaw_moving);
            }
            new_pitch_current = pitch_local.current_deg;
            new_pitch_moving = pitch_local.moving;
            if (pitch_linear_mode) {
                AdvanceAxisLinear(pitch_local, now_ms,
                                  new_pitch_current, new_pitch_moving);
            } else {
                AdvanceAxisSpring(pitch_local, pitch_anim_, pitch_snap_on_rest_,
                                  dt_s, new_pitch_current, new_pitch_moving);
            }
            xSemaphoreGive(motion_mutex_);

            // Known carve-out (#161): motion_mutex_ is released here
            // and re-acquired after the WritePos block. If StartMove
            // or InvalidateAxisToken (Phase 0' / torque disable) fires
            // inside this release window, the WritePos calls below
            // still send a stale interpolation step on the bus. The
            // post-bus request_token guard below then correctly skips
            // the current_deg / moving commit, but the physical
            // intermediate position has already been issued. The
            // pre-PR move_start_ms guard had the same surface; this PR
            // does not regress that behavior. Closing the pre-bus gate
            // is tracked separately under #161.
            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
            if (yaw_local.moving) {
                int yaw_pos = YawDegToPos(new_yaw_current);
                int r = scs_bus_.WritePos(SERVO_YAW_ID, yaw_pos, MOTION_PER_WRITE_TIME_MS, 0);
                if (!ServoWritePosOk(r)) {
                    ESP_LOGW(TAG, "Motion yaw WritePos failed: r=%d (deg=%d, pos=%d)",
                             r, new_yaw_current, yaw_pos);
                }
            }
            vTaskDelay(kInterFrameGap);
            if (pitch_local.moving) {
                int pitch_pos = PitchDegToPos(new_pitch_current);
                int r = scs_bus_.WritePos(SERVO_PITCH_ID, pitch_pos, MOTION_PER_WRITE_TIME_MS, 0);
                if (!ServoWritePosOk(r)) {
                    ESP_LOGW(TAG, "Motion pitch WritePos failed: r=%d (deg=%d, pos=%d)",
                             r, new_pitch_current, pitch_pos);
                }
            }
            xSemaphoreGive(scs_bus_mutex_);

            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            if (yaw_motion_.request_token == yaw_local.request_token) {
                yaw_motion_.current_deg = new_yaw_current;
            }
            if (!new_yaw_moving && yaw_motion_.target_deg == yaw_local.target_deg
                && yaw_motion_.request_token == yaw_local.request_token) {
                yaw_motion_.moving = false;
            }
            if (pitch_motion_.request_token == pitch_local.request_token) {
                pitch_motion_.current_deg = new_pitch_current;
            }
            if (!new_pitch_moving && pitch_motion_.target_deg == pitch_local.target_deg
                && pitch_motion_.request_token == pitch_local.request_token) {
                pitch_motion_.moving = false;
            }
            xSemaphoreGive(motion_mutex_);
        }

        // Bump the request token for the specified axis. Used by
        // InitializeServo's Phase 0' re-sync so a Tick() snapshot
        // taken before the re-sync no longer passes the post-bus
        // freshness guard (which would otherwise overwrite the just-
        // re-synced current_deg / moving state). Caller must hold
        // motion_mutex_; this method does not take it.
        void InvalidateAxisToken(int axis_id) override {
            if (axis_id == SERVO_YAW_ID) {
                yaw_motion_.request_token = ++next_request_token_;
            } else if (axis_id == SERVO_PITCH_ID) {
                pitch_motion_.request_token = ++next_request_token_;
            }
        }

    private:
        static void StartAxisSpring(
            smooth_ui_toolkit::AnimateValue& axis_anim,
            bool& snap_on_rest,
            int current_deg,
            int target_deg,
            bool moving,
            const smooth_ui_toolkit::SpringOptions_t& spring_options) {
            if (!moving) {
                axis_anim.teleport(static_cast<float>(current_deg));
                snap_on_rest = false;
                return;
            }

            axis_anim.springOptions() = spring_options;
            axis_anim.teleport(static_cast<float>(current_deg));
            axis_anim = static_cast<float>(target_deg);
            snap_on_rest = true;
        }

        static void AdvanceAxisSpring(
            const AxisMotion& axis_local,
            smooth_ui_toolkit::AnimateValue& axis_anim,
            bool& snap_on_rest,
            float dt_s,
            int& new_current_deg,
            bool& new_moving) {
            if (!axis_local.moving) {
                return;
            }

            axis_anim.updateWithDelta(dt_s);
            new_current_deg = static_cast<int>(axis_anim.directValue());
            if (axis_anim.done()) {
                new_moving = false;
                if (snap_on_rest) {
                    new_current_deg = static_cast<int>(axis_anim.end);
                    snap_on_rest = false;
                }
            }
        }

        static void AdvanceAxisLinear(
            const AxisMotion& axis_local,
            uint32_t now_ms,
            int& new_current_deg,
            bool& new_moving) {
            if (!axis_local.moving) {
                return;
            }

            // Linear mode intentionally stays wall-clock based, matching the
            // pre-spring interpolation path used for boot-init slow climbs;
            // spring mode uses the real elapsed Tick delta instead.
            uint32_t elapsed = now_ms - axis_local.move_start_ms;
            if (axis_local.move_duration_ms == 0 ||
                elapsed >= axis_local.move_duration_ms) {
                new_current_deg = axis_local.target_deg;
                new_moving = false;
            } else {
                int delta = axis_local.target_deg - axis_local.start_deg;
                new_current_deg = axis_local.start_deg +
                    static_cast<int>(
                        static_cast<int64_t>(delta) * elapsed /
                        axis_local.move_duration_ms);
            }
        }

        ScsBus& scs_bus_;
        SemaphoreHandle_t& scs_bus_mutex_;
        SemaphoreHandle_t& motion_mutex_;
        AxisMotion& yaw_motion_;
        AxisMotion& pitch_motion_;
        // Monotonically increasing request id. Each StartMove increments
        // this and writes the new value into yaw_motion_.request_token
        // and pitch_motion_.request_token. Tick() snapshots both fields
        // with the rest of AxisMotion and uses request_token equality
        // (rather than move_start_ms, which only has ms resolution) to
        // detect whether a snapshot is still the live request.
        // InvalidateAxisToken() also bumps this counter for board-level
        // direct AxisMotion resets that do not go through StartMove
        // (currently InitializeServo's Phase 0' post-init ReadPos
        // re-sync and the set_servo_torque disable path); without that
        // bump, a Tick() snapshot taken before such a reset would pass
        // the post-bus freshness guard and overwrite the just-reset
        // state. motion_mutex_ guards this counter. The pre-bus
        // stale-WritePos race, where an external reset lands after the
        // snapshot but before the bus frame, is tracked separately
        // under #161.
        uint64_t next_request_token_ = 0;
        smooth_ui_toolkit::AnimateValue yaw_anim_;
        smooth_ui_toolkit::AnimateValue pitch_anim_;
        bool yaw_snap_on_rest_ = false;
        bool pitch_snap_on_rest_ = false;
        bool yaw_linear_mode_ = false;
        bool pitch_linear_mode_ = false;
        uint64_t last_tick_us_ = 0;
    };

    class ServoDelegatedMotionDriver final : public MotionDriver {
    public:
        ServoDelegatedMotionDriver(ScsBus& scs_bus,
                                   SemaphoreHandle_t& scs_bus_mutex,
                                   SemaphoreHandle_t& motion_mutex,
                                   AxisMotion& yaw_motion,
                                   AxisMotion& pitch_motion)
            : motion_mutex_(motion_mutex),
              yaw_motion_(yaw_motion),
              pitch_motion_(pitch_motion),
              next_request_token_(0),
              yaw_axis_(SERVO_YAW_ID, YawDegToPos, "yaw", yaw_motion,
                        scs_bus, scs_bus_mutex, motion_mutex,
                        next_request_token_,
                        /* post_dispatch_quiet_gap_ms = */ 10),
              pitch_axis_(SERVO_PITCH_ID, PitchDegToPos, "pitch",
                          pitch_motion, scs_bus, scs_bus_mutex,
                          motion_mutex, next_request_token_,
                          /* post_dispatch_quiet_gap_ms = */ 0) {}

        // StartMove only mutates AxisMotion state (under the caller's
        // motion_mutex_ per the MotionDriver::StartMove contract) and marks
        // each non-noop axis as having a pending dispatch. The actual WritePos
        // is performed by Tick() on the servo_motion task, so callers running
        // on timer tasks (e.g. TouchPollCb -> StartServoWobble) never block
        // on UART I/O. This mirrors HostInterpolationMotionDriver's
        // "StartMove writes state; Tick drives the bus" split and keeps
        // timer-task latency bounded.
        void StartMove(float yaw_deg, float pitch_deg,
                       uint32_t duration_ms,
                       bool prefer_linear) override {
            // The delegated path is already duration-bounded by the SCS0009
            // internal interpolation time argument; there is no host-side
            // profile to switch.
            (void)prefer_linear;
            uint16_t clamped = clamp_u16(duration_ms);
            if (duration_ms > clamped) {
                static bool duration_overflow_warned = false;
                if (!duration_overflow_warned) {
                    ESP_LOGW(TAG,
                             "Servo-delegated motion duration overflow: axis=yaw/pitch requested_ms=%u clamped_ms=%u",
                             (unsigned)duration_ms, (unsigned)clamped);
                    duration_overflow_warned = true;
                } else {
                    ESP_LOGD(TAG,
                             "Servo-delegated motion duration overflow: axis=yaw/pitch requested_ms=%u clamped_ms=%u",
                             (unsigned)duration_ms, (unsigned)clamped);
                }
            }

            // Per-axis no-op detection: when the axis is idle AND the request
            // matches the last-known current_deg AND the position is not
            // marked unknown, skip staging a dispatch. Issuing WritePos in
            // that case would start a delegated motion toward host-side
            // current_deg, which may diverge from the physical position
            // (e.g. after a boot ReadPos-failure path where current_deg was
            // seeded to BOOT_INIT_* via the safe fallback but the head sits
            // elsewhere). HostInterpolation path keeps its always-WritePos
            // behaviour for backward compatibility.
            //
            // position_unknown is the recovery signal from a prior ReadMove
            // force-clear: current_deg holds the requested target but
            // physical completion was never confirmed, so we MUST re-dispatch
            // (even when target == current_deg) to surface a persistent
            // failure rather than silently treat the axis as at-target.
            //
            // motion_mutex_ is held by the WriteHeadAngles caller per the
            // MotionDriver::StartMove contract (declared at the base class).
            // Taking it again here would deadlock the non-recursive FreeRTOS
            // semaphore — Stage() reads its AxisMotion directly.
            yaw_axis_.Stage(static_cast<int>(yaw_deg), clamped);
            pitch_axis_.Stage(static_cast<int>(pitch_deg), clamped);
        }

        float GetYawDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int yaw = yaw_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(yaw);
        }

        float GetPitchDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int pitch = pitch_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(pitch);
        }

        bool IsMoving() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            bool moving = yaw_motion_.moving || pitch_motion_.moving;
            xSemaphoreGive(motion_mutex_);
            return moving;
        }

        void Tick() override {
            vTaskDelay(pdMS_TO_TICKS(MOTION_POLL_INTERVAL_MS));

            // Per-axis update. Post-bus-frame quiet period is held INSIDE
            // each axis's Dispatch() (WritePos) and PollReadMove()
            // (ReadMove) atomically with the bus frame itself (no release
            // / reacquire window where concurrent MCP callers could inject
            // bus frames). yaw is configured with 10 ms via
            // post_dispatch_quiet_gap_ms_ (the member name pre-dates the
            // post-ReadMove path but the value applies symmetrically to
            // both frame types); pitch is configured with 0 ms matching
            // the PR #146 empirical model.
            //
            // Inter-axis quiet period coverage:
            // - yaw Dispatch tick (WritePos): in-Dispatch 10 ms hold
            //   provides inter-axis spacing before pitch_axis_.Update().
            // - yaw PollReadMove tick (ReadMove): in-PollReadMove 10 ms
            //   hold provides the same inter-axis spacing — prevents the
            //   ReadMove -> WritePos 0 ms inter-frame sequence on the
            //   shared SCS bus that Phase 2's per-axis grain would
            //   otherwise expose (PR #146 had no such ordering because
            //   dispatch and poll Tick phases were mutually exclusive).
            // - yaw no-op tick: no yaw bus frame, no quiet period needed;
            //   pitch_axis_.Update() runs immediately.
            //
            // Two-axis simultaneous dispatch (the necessary side of the
            // PR #146 E2 / E4-cumulative hang trigger) remains
            // structurally eliminated: yaw and pitch each take
            // scs_bus_mutex_ in their own separate short-hold critical
            // section inside Update(), and pitch_axis_.Update() runs
            // only after yaw_axis_.Update() returns (i.e. after yaw's
            // scs_bus_mutex_ hold has been released).
            //
            // No inter-axis vTaskDelay at this wrapper level: every yaw
            // bus-frame-emitting branch already provides the 10 ms
            // wall-clock spacing inside its in-Method hold. The 10 ms
            // inter-frame budget also remains in the unchanged
            // HostInterpolationMotionDriver::Tick path.
            yaw_axis_.Update();
            pitch_axis_.Update();
        }

        // Caller holds motion_mutex_. Bumps next_request_token_ for the
        // specified axis (mirrors HostInterpolationMotionDriver's
        // implementation) AND clears the per-AxisServo private
        // cancellation state via OnExternalReset(), so that a Stage()
        // call that preceded the external mutation does not leak a stale
        // WritePos onto the bus from the next Update() tick.
        //
        // Used by InitializeServo's Phase 0' post-init ReadPos re-sync
        // and by the set_servo_torque MCP tool's disable path.
        //
        // Closing this cancellation boundary required a paired AxisMotion
        // (visible) + AxisServo (driver-private) atomic reset: bumping
        // the visible request_token alone (the default no-op fallback
        // this driver used to inherit) was insufficient because
        // pending_dispatch_ / dispatch_failures_ / readmove_failures_
        // survived a Phase 0' direct AxisMotion mutation and the next
        // Update() tick would re-issue a WritePos for the stale staged
        // target. Issue #160 tracks the design discussion and the
        // adversarial review that converged on this Option A design.
        //
        // Scope note: this closes the post-Update / next-tick race only.
        // A snapshot taken by Update() BEFORE the external reset still
        // carries a local `dispatch = true` boolean (Update()'s local
        // variable, not the member field) that survives this member-
        // state clear. The in-flight Dispatch() then passes the
        // request_token freshness gate at the pre-WritePos check (which
        // now sees a bumped token) and skips the WritePos itself, so
        // the bus is not touched with a stale frame — but the call
        // still acquires scs_bus_mutex_ once before returning. The
        // pre-bus stale-command race (Issue #161) is the remaining
        // cancellation-boundary layer and is intentionally NOT closed
        // by this PR.
        void InvalidateAxisToken(int axis_id) override {
            if (axis_id == SERVO_YAW_ID) {
                yaw_motion_.request_token = ++next_request_token_;
                yaw_axis_.OnExternalReset();
            } else if (axis_id == SERVO_PITCH_ID) {
                pitch_motion_.request_token = ++next_request_token_;
                pitch_axis_.OnExternalReset();
            }
        }

    private:
        static constexpr int kReadMoveFailureLimit = 5;
        static constexpr int kDispatchFailureLimit = 5;
        // Settle margin past move_duration_ms before treating a
        // stuck-high ReadMove (servo returns 1 forever after the
        // requested completion) as a failure. Without this bound,
        // a degraded servo / register path would keep moving=true
        // indefinitely; wobble would never advance and same-target
        // recovery would never run.
        static constexpr uint32_t kReadMoveStuckMarginMs = 1000;
        // Margin below move_duration_ms in which an early ReadMove==0
        // is allowed as a genuine completion (the SCS0009's internal
        // interpolation can finish slightly early). A ReadMove==0
        // arriving further before the commanded completion is
        // implausible and likely a stuck-low / false-zero status read
        // from a degraded register path; treat it as suspicious and
        // mark position_unknown so the next StartMove forces a fresh
        // dispatch instead of trusting the stale optimistic commit.
        static constexpr uint32_t kReadMoveEarlyMarginMs = 200;

        class AxisServo {
            // Lock-order audit for Update():
            // - Snapshot: motion_mutex_ only.
            // - Dispatch freshness gate: scs_bus_mutex_ -> motion_mutex_;
            //   motion_mutex_ is released before WritePos.
            // - Dispatch commit: motion_mutex_ only after scs_bus_mutex_ is
            //   released.
            // - ReadMove poll: scs_bus_mutex_ only, then motion_mutex_ only
            //   for commit. No path takes motion_mutex_ before scs_bus_mutex_.

        public:
            AxisServo(uint8_t servo_id, int (*deg_to_pos)(int),
                      const char* axis_name, AxisMotion& motion,
                      ScsBus& scs_bus, SemaphoreHandle_t& scs_bus_mutex,
                      SemaphoreHandle_t& motion_mutex,
                      uint64_t& next_request_token,
                      uint32_t post_dispatch_quiet_gap_ms)
                : servo_id_(servo_id),
                  deg_to_pos_(deg_to_pos),
                  axis_name_(axis_name),
                  motion_(motion),
                  scs_bus_(scs_bus),
                  scs_bus_mutex_(scs_bus_mutex),
                  motion_mutex_(motion_mutex),
                  next_request_token_(next_request_token),
                  post_dispatch_quiet_gap_ms_(post_dispatch_quiet_gap_ms) {}

            void Stage(int target_deg, uint16_t duration_ms) {
                // motion_mutex_ is held by the WriteHeadAngles caller per
                // the MotionDriver::StartMove contract. Taking it again here
                // would deadlock the non-recursive FreeRTOS semaphore.
                bool noop =
                    !motion_.moving && !motion_.position_unknown &&
                    target_deg == motion_.current_deg;
                if (noop) {
                    return;
                }

                uint32_t now_ms =
                    static_cast<uint32_t>(esp_timer_get_time() / 1000);
                motion_.start_deg = motion_.current_deg;
                motion_.target_deg = target_deg;
                motion_.move_start_ms = now_ms;
                // dispatch_start_ms stays 0 until FinishDispatch confirms
                // a WritePos ACK. ApplyReadMoveResult's stuck-high timeout
                // skips the check while dispatch_start_ms is 0, so retry
                // latency does not eat into the servo-internal duration
                // budget.
                motion_.dispatch_start_ms = 0;
                motion_.move_duration_ms = duration_ms;
                motion_.moving = true;
                motion_.request_token = ++next_request_token_;
                // NOTE: position_unknown is NOT cleared here. It is cleared
                // by FinishDispatch only after a successful WritePos ACK.
                // Clearing it on stage would let dispatch retry exhaustion
                // leave position_unknown=false despite no confirmed physical
                // motion; then the next same-target StartMove would no-op
                // skip on current_deg==target_deg and hide the bus failure.
                pending_dispatch_ = true;
                // Fresh request: reset the per-axis dispatch retry budget so
                // previous failures do not shorten this request's runway.
                dispatch_failures_ = 0;
            }

            // Returns true if this tick emitted any bus frame on the SCS
            // bus (WritePos via Dispatch() or ReadMove via PollReadMove()).
            // The wrapper Tick() does not use the bool to apply any
            // additional hold — both Dispatch() and PollReadMove() each
            // hold scs_bus_mutex_ atomically across their bus frame AND
            // the post-frame post_dispatch_quiet_gap_ms_ (yaw: 10 ms,
            // pitch: 0 ms), so the inter-axis quiet period is enforced
            // inside each axis's method without any release/reacquire
            // window. The return value is informational (kept for
            // diagnostic clarity and potential future use).
            bool Update() {
                AxisMotion snapshot;
                bool dispatch = false;
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                snapshot = motion_;
                dispatch = pending_dispatch_;
                // Do NOT clear pending_dispatch here. FinishDispatch consumes
                // it only on success or retry exhaustion; transient WritePos
                // failures keep it true so the next tick retries the same
                // target instead of silently dropping the request.
                xSemaphoreGive(motion_mutex_);

                // With per-axis Update(), dispatch-vs-poll is chosen per
                // servo. One axis can spend this tick dispatching while the
                // other axis polls ReadMove after the wrapper's inter-axis
                // wall-clock gap.
                if (dispatch) {
                    return Dispatch(snapshot);
                }
                if (snapshot.moving) {
                    PollReadMove(snapshot);
                }
                return false;
            }

            // Caller must hold motion_mutex_. Clears the per-axis private
            // cancellation state so that a subsequent Update() tick observes
            // a clean slate after the board-level code directly mutates
            // AxisMotion outside the Stage() path (currently
            // InitializeServo's Phase 0' post-init ReadPos re-sync and the
            // set_servo_torque MCP tool's disable path).
            //
            // Does NOT take any semaphore (motion_mutex_ is already held by
            // the caller; double-take of the non-recursive FreeRTOS
            // semaphore would deadlock). Does NOT touch the SCS bus. Does
            // NOT touch motion_ (AxisMotion); the caller has already mutated
            // it before invoking this method through
            // ServoDelegatedMotionDriver::InvalidateAxisToken.
            //
            // INVARIANT: every new AxisServo private cancellation-state
            // field added in the future MUST be added to this reset.
            // Otherwise external resets (Phase 0' / torque disable / any
            // future cancellation caller) will leave stale state that the
            // next Update() may act on, regressing the Issue #160 fix.
            void OnExternalReset() {
                pending_dispatch_ = false;
                dispatch_failures_ = 0;
                readmove_failures_ = 0;
            }

        private:
            // Returns true if WritePos was actually issued on the bus
            // (i.e. the snapshot was still the live request at the
            // pre-WritePos freshness gate). Returns false when a newer
            // StartMove superseded the snapshot between Update's
            // motion_mutex_ release and Dispatch's freshness gate — in
            // that case the bus was not touched, and the wrapper Tick()
            // does not need to hold scs_bus_mutex_ across the
            // inter-frame gap.
            bool Dispatch(const AxisMotion& snapshot) {
                int result = 0;
                bool live = false;
                int pos = deg_to_pos_(snapshot.target_deg);
                uint16_t duration =
                    static_cast<uint16_t>(snapshot.move_duration_ms);

                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                live = motion_.request_token == snapshot.request_token;
                xSemaphoreGive(motion_mutex_);
                if (live) {
                    result = scs_bus_.WritePos(servo_id_, pos, duration, 0);
                    // Hold scs_bus_mutex_ across the post-WritePos quiet
                    // period atomically with the WritePos itself. Without
                    // this, releasing the mutex here would expose a
                    // release/reacquire window where concurrent MCP
                    // callers (get_head_angles ReadPos, uart_diag raw
                    // frames) could acquire the bus and inject traffic
                    // before any wrapper-level quiet-period guard
                    // starts. The original PR #146 bundled critical
                    // section incidentally protected this window;
                    // Phase 2's per-axis short-hold grain restores it
                    // per axis instead. Skipped when the quiet gap is
                    // 0 ms (pitch axis) or the WritePos was superseded
                    // (!live) — see post_dispatch_quiet_gap_ms_ member
                    // comment for per-axis policy rationale.
                    if (post_dispatch_quiet_gap_ms_ > 0) {
                        vTaskDelay(pdMS_TO_TICKS(post_dispatch_quiet_gap_ms_));
                    }
                }
                xSemaphoreGive(scs_bus_mutex_);

                bool write_ok = !live || ServoWritePosOk(result);
                if (live && !write_ok) {
                    ESP_LOGW(TAG,
                             "Motion %s WritePos failed: r=%d (deg=%d, pos=%d)",
                             axis_name_, result, snapshot.target_deg, pos);
                }

                // dispatch_now_ms captures the time WritePos completed
                // (ACK or timeout). FinishDispatch uses this for
                // dispatch_start_ms on success, so ApplyReadMoveResult
                // measures from physical acceptance rather than staging.
                uint32_t dispatch_now_ms =
                    static_cast<uint32_t>(esp_timer_get_time() / 1000);

                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                // Commit / consume pending only if the snapshot is still
                // live and the WritePos actually ran. Superseded dispatches
                // keep the new request's pending flag intact and reset only
                // the stale retry counter.
                if (live && motion_.request_token == snapshot.request_token) {
                    FinishDispatch(snapshot.target_deg, write_ok,
                                   dispatch_now_ms);
                } else {
                    dispatch_failures_ = 0;
                }
                xSemaphoreGive(motion_mutex_);

                return live;
            }

            void PollReadMove(const AxisMotion& snapshot) {
                int read_move = -1;
                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                read_move = scs_bus_.ReadMove(servo_id_);
                // Hold scs_bus_mutex_ across the post-ReadMove quiet
                // period atomically with the ReadMove itself, mirroring
                // the post-WritePos pattern in Dispatch(). Without this,
                // releasing the mutex here would expose a window where
                // the wrapper Tick()'s subsequent pitch_axis_.Update()
                // could enter Dispatch() and issue a pitch WritePos
                // immediately, creating a yaw-ReadMove -> pitch-WritePos
                // sequence with effectively 0 ms inter-frame spacing on
                // the shared SCS bus. PR #146's bundled critical section
                // separated dispatch ticks from poll ticks (Tick step 1
                // vs step 2 mutually exclusive), so this ReadMove ->
                // WritePos ordering never arose; Phase 2's per-axis
                // grain makes it possible, so the guard is restored
                // per axis here. Skipped when post_dispatch_quiet_gap_ms_
                // is 0 (pitch axis); the member is shared between
                // post-WritePos and post-ReadMove paths because the
                // bus-quiet rationale is identical for both frame types.
                if (post_dispatch_quiet_gap_ms_ > 0) {
                    vTaskDelay(pdMS_TO_TICKS(post_dispatch_quiet_gap_ms_));
                }
                xSemaphoreGive(scs_bus_mutex_);

                uint32_t now_ms =
                    static_cast<uint32_t>(esp_timer_get_time() / 1000);

                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                // request_token guards against a newer StartMove racing in
                // between the Update snapshot and this commit; ms-resolution
                // move_start_ms can collide for back-to-back requests.
                if (motion_.request_token == snapshot.request_token) {
                    ApplyReadMoveResult(read_move, now_ms);
                }
                xSemaphoreGive(motion_mutex_);
            }

            // Finalises a dispatched WritePos. Caller holds motion_mutex_.
            // - write_ok==true: commit current_deg = target, consume the
            //   pending_dispatch flag, reset dispatch + ReadMove failure
            //   counters. ReadMove poll then tracks the in-flight delegated
            //   motion to completion.
            // - write_ok==false: keep pending_dispatch=true so the next tick
            //   retries the same target (transient ACK timeout / UART error
            //   should not silently drop the request). Bound the retry by
            //   kDispatchFailureLimit; when exhausted, log once, consume
            //   pending_dispatch, and clear moving so the axis returns to
            //   idle rather than spinning the retry loop forever.
            // readmove_failures_ is reset on success only; it tracks ReadMove
            // polling and is independent of WritePos ack semantics.
            void FinishDispatch(int target_deg, bool write_ok,
                                uint32_t dispatch_now_ms) {
                if (write_ok) {
                    motion_.current_deg = target_deg;
                    // dispatch_start_ms records when the servo actually
                    // received the GOAL_POSITION / GOAL_TIME write. This
                    // (not the staging timestamp in move_start_ms) is what
                    // ApplyReadMoveResult uses for the stuck-high timeout,
                    // so degraded-bus dispatch latency doesn't eat into
                    // the servo-internal duration budget.
                    motion_.dispatch_start_ms = dispatch_now_ms;
                    // Confirmed WritePos ACK supersedes any prior
                    // position_unknown mark. The new WritePos+ReadMove
                    // cycle is what proves (or fails to prove) the
                    // physical position.
                    motion_.position_unknown = false;
                    pending_dispatch_ = false;
                    dispatch_failures_ = 0;
                    readmove_failures_ = 0;
                    return;
                }
                dispatch_failures_++;
                if (dispatch_failures_ >= kDispatchFailureLimit) {
                    ESP_LOGW(TAG,
                             "Motion %s WritePos retries exhausted: current_deg=%d target_deg=%d; %d consecutive dispatch failures, abandoning request and marking position unknown",
                             axis_name_, motion_.current_deg,
                             motion_.target_deg, kDispatchFailureLimit);
                    pending_dispatch_ = false;
                    motion_.moving = false;
                    // A WritePos ACK timeout is NOT proof that the servo
                    // ignored the command — the command may have reached
                    // the servo while only the ACK/readback path failed.
                    // In that case the physical head has already moved to
                    // target_deg, but current_deg still holds the old
                    // value. Without position_unknown=true here, the next
                    // same-old-position StartMove would no-op-skip on the
                    // stale current_deg and silently drop the recovery
                    // request — exactly the degraded-bus condition this
                    // path is meant to handle. Mark unknown so a same-
                    // target retry forces a fresh dispatch and either
                    // confirms (FinishDispatch write_ok clears the flag)
                    // or surfaces another failure.
                    motion_.position_unknown = true;
                    dispatch_failures_ = 0;
                }
                // else: pending_dispatch stays true; next Update will retry the
                // same target (same start_deg / move_start_ms / move_duration_ms).
            }

            void ApplyReadMoveResult(int read_move, uint32_t now_ms) {
                if (read_move >= 0) {
                    readmove_failures_ = 0;
                    if (read_move == 0) {
                        // Sanity check against a stuck-low / false-zero
                        // status register: FinishDispatch optimistically
                        // committed current_deg to target_deg on WritePos
                        // ACK, so a transient ReadMove==0 returned before
                        // the servo could physically reach target would
                        // make the host treat the axis as "at target" and
                        // let the next same-target StartMove no-op-skip.
                        // If the elapsed time since confirmed dispatch is
                        // implausibly short relative to the commanded
                        // move_duration_ms (allowing kReadMoveEarlyMarginMs
                        // for genuine early arrival), treat the zero as
                        // suspicious and mark the position unknown.
                        if (motion_.dispatch_start_ms != 0 &&
                            motion_.move_duration_ms > kReadMoveEarlyMarginMs) {
                            uint32_t elapsed = now_ms - motion_.dispatch_start_ms;
                            uint32_t plausible_min =
                                motion_.move_duration_ms - kReadMoveEarlyMarginMs;
                            if (elapsed < plausible_min) {
                                ESP_LOGW(TAG,
                                         "Motion %s ReadMove=0 implausibly early: current_deg=%d target_deg=%d, elapsed=%ums but commanded duration=%ums (early margin=%ums); marking position unknown",
                                         axis_name_, motion_.current_deg,
                                         motion_.target_deg, (unsigned)elapsed,
                                         (unsigned)motion_.move_duration_ms,
                                         (unsigned)kReadMoveEarlyMarginMs);
                                motion_.moving = false;
                                motion_.position_unknown = true;
                                return;
                            }
                        }
                        motion_.moving = false;
                        return;
                    }
                    // read_move > 0: servo reports still moving. Bound the
                    // wait by move_duration_ms + kReadMoveStuckMarginMs to
                    // guard against a stuck-high ReadMove (the servo or
                    // register path degrades such that the motion-status
                    // bit never clears even after the requested completion
                    // time has elapsed).
                    //
                    // Elapsed is measured from dispatch_start_ms (when the
                    // servo actually received the command via a successful
                    // WritePos ACK), not from move_start_ms (staging time),
                    // so degraded-bus dispatch latency does not cause
                    // premature force-clear while the servo is genuinely
                    // still mid-motion. If dispatch_start_ms is still 0 the
                    // WritePos has not yet ACK'd; skip the timeout check
                    // until the dispatch is confirmed. Unsigned subtraction
                    // stays wrap-safe across the uint32 ms counter.
                    if (motion_.dispatch_start_ms == 0) {
                        return;
                    }
                    uint32_t elapsed = now_ms - motion_.dispatch_start_ms;
                    if (elapsed > motion_.move_duration_ms + kReadMoveStuckMarginMs) {
                        ESP_LOGW(TAG,
                                 "Motion %s ReadMove stuck-high: current_deg=%d target_deg=%d, %ums past commanded completion; marking position unknown and force-clearing moving",
                                 axis_name_, motion_.current_deg,
                                 motion_.target_deg,
                                 (unsigned)(elapsed - motion_.move_duration_ms));
                        motion_.moving = false;
                        motion_.position_unknown = true;
                    }
                    return;
                }

                readmove_failures_++;
                if (readmove_failures_ >= kReadMoveFailureLimit) {
                    ESP_LOGW(TAG,
                             "Motion %s ReadMove failed: current_deg=%d target_deg=%d; %d consecutive ReadMove failures, marking position unknown and force-clearing moving (next StartMove will re-dispatch even if target matches current_deg)",
                             axis_name_, motion_.current_deg,
                             motion_.target_deg, kReadMoveFailureLimit);
                    motion_.moving = false;
                    // Without this flag, a subsequent same-target StartMove
                    // would no-op-skip on current_deg==target_deg and the
                    // bus failure would stay hidden behind the optimistic
                    // commit. Marking the position unknown forces the next
                    // StartMove to re-dispatch and surface (or recover from)
                    // the underlying ReadMove fault.
                    motion_.position_unknown = true;
                    readmove_failures_ = 0;
                }
            }

            uint8_t servo_id_;
            int (*deg_to_pos_)(int);
            const char* axis_name_;
            AxisMotion& motion_;
            ScsBus& scs_bus_;
            SemaphoreHandle_t& scs_bus_mutex_;
            SemaphoreHandle_t& motion_mutex_;
            uint64_t& next_request_token_;
            // Post-bus-frame quiet period held INSIDE scs_bus_mutex_ on
            // a successful bus operation. Applies to BOTH frame types:
            // - Dispatch() WritePos: scs_bus_mutex_ is not released between
            //   the WritePos and this vTaskDelay.
            // - PollReadMove() ReadMove: scs_bus_mutex_ is not released
            //   between the ReadMove and this vTaskDelay.
            //
            // yaw is configured with 10 ms to preserve the SCS bus quiet
            // period that the original PR #146 bundled critical section
            // incidentally protected — for WritePos -> next-frame ordering
            // (PR #146 empirical model) AND for the new ReadMove ->
            // pitch-WritePos ordering introduced by Phase 2's per-axis
            // grain (PR #146 had no such ordering because dispatch and
            // poll Tick phases were mutually exclusive).
            //
            // pitch is configured with 0 ms because the PR #146 empirical
            // model (E1 / E4-fresh / E5 / E6 all clean) shows post-pitch
            // quiet was not required for bus stability. Set to 0 to
            // disable the per-axis post-frame hold entirely. Holding
            // scs_bus_mutex_ across a vTaskDelay is intentional here —
            // it blocks concurrent MCP bus callers (get_head_angles
            // ReadPos, uart_diag raw frames) for the quiet-period
            // duration, which is the explicit invariant being restored.
            //
            // Name retained as "post_dispatch_quiet_gap_ms_" for
            // historical continuity; semantically it is "post-bus-frame
            // quiet gap" and applies symmetrically to both WritePos and
            // ReadMove paths.
            uint32_t post_dispatch_quiet_gap_ms_;
            int readmove_failures_ = 0;
            bool pending_dispatch_ = false;
            int dispatch_failures_ = 0;
        };

        SemaphoreHandle_t& motion_mutex_;
        AxisMotion& yaw_motion_;
        AxisMotion& pitch_motion_;
        // Monotonically increasing request id. Each StartMove that stages
        // a dispatch picks ++next_request_token_ and writes it into the
        // corresponding AxisMotion::request_token. Tick() then uses
        // request_token equality (rather than move_start_ms, which only
        // has ms resolution) to detect whether a snapshot is still the
        // live request. motion_mutex_ guards this counter.
        uint64_t next_request_token_ = 0;
        AxisServo yaw_axis_;
        AxisServo pitch_axis_;
    };

    void InitializePowerSaveTimer() {
        power_save_timer_ = new PowerSaveTimer(-1, 60, 300);
        power_save_timer_->OnEnterSleepMode([this]() {
            GetDisplay()->SetPowerSaveMode(true);
            GetBacklight()->SetBrightness(10);
        });
        power_save_timer_->OnExitSleepMode([this]() {
            GetDisplay()->SetPowerSaveMode(false);
            GetBacklight()->RestoreBrightness();
        });
        power_save_timer_->OnShutdownRequest([this]() {
            pmic_->PowerOff();
        });
        power_save_timer_->SetEnabled(true);
    }

    void InitializeI2c() {
        // Initialize I2C peripheral
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = (i2c_port_t)1,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));
    }

    void InitializePortAI2c() {
        // Grove Port A bus. Uses I2C controller 0 (the internal bus above
        // uses controller 1) so the two run independently. Attached Unit
        // modules typically include their own 10 kΩ pull-ups in the Grove
        // hub, but enable internal pull-ups as a fall-back for bare wiring.
        i2c_master_bus_config_t port_a_cfg = {
            .i2c_port = (i2c_port_t)0,
            .sda_io_num = PORT_A_I2C_SDA_PIN,
            .scl_io_num = PORT_A_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&port_a_cfg, &port_a_i2c_bus_));
    }

    void I2cDetect() {
        uint8_t address;
        printf("     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\r\n");
        for (int i = 0; i < 128; i += 16) {
            printf("%02x: ", i);
            for (int j = 0; j < 16; j++) {
                fflush(stdout);
                address = i + j;
                esp_err_t ret = i2c_master_probe(i2c_bus_, address, pdMS_TO_TICKS(200));
                if (ret == ESP_OK) {
                    printf("%02x ", address);
                } else if (ret == ESP_ERR_TIMEOUT) {
                    printf("UU ");
                } else {
                    printf("-- ");
                }
            }
            printf("\r\n");
        }
    }

    void InitializeAxp2101() {
        ESP_LOGI(TAG, "Init AXP2101");
        pmic_ = new Pmic(i2c_bus_, 0x34);
    }

    void InitializeAw9523() {
        ESP_LOGI(TAG, "Init AW9523");
        aw9523_ = new Aw9523(i2c_bus_, 0x58);
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    void PollTouchpad() {
        static bool was_touched = false;
        static int64_t touch_start_time = 0;
        const int64_t TOUCH_THRESHOLD_MS = 500;  // 触摸时长阈值，超过500ms视为长按
        
        ft6336_->UpdateTouchPoint();
        auto& touch_point = ft6336_->GetTouchPoint();
        
        // 检测触摸开始
        if (touch_point.num > 0 && !was_touched) {
            was_touched = true;
            touch_start_time = esp_timer_get_time() / 1000; // 转换为毫秒
        } 
        // 检测触摸释放
        else if (touch_point.num == 0 && was_touched) {
            was_touched = false;
            int64_t touch_duration = (esp_timer_get_time() / 1000) - touch_start_time;
            
            // 只有短触才触发
            if (touch_duration < TOUCH_THRESHOLD_MS) {
                auto& app = Application::GetInstance();
                if (app.GetDeviceState() == kDeviceStateStarting) {
                    EnterWifiConfigMode();
                    return;
                }
                app.ToggleChatState();
            }
        }
    }

    void InitializeFt6336TouchPad() {
        ESP_LOGI(TAG, "Init FT6336");
        ft6336_ = new Ft6336(i2c_bus_, 0x38);
        
        // 创建定时器，20ms 间隔
        esp_timer_create_args_t timer_args = {
            .callback = [](void* arg) {
                StackChanBoard* board = (StackChanBoard*)arg;
                board->PollTouchpad();
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "touchpad_timer",
            .skip_unhandled_events = true,
        };
        
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &touchpad_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(touchpad_timer_, 20 * 1000));
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = GPIO_NUM_37;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = GPIO_NUM_36;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeIli9342Display() {
        ESP_LOGI(TAG, "Init IlI9342");

        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;

        ESP_LOGD(TAG, "Install panel IO");
        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = GPIO_NUM_3;
        io_config.dc_gpio_num = GPIO_NUM_35;
        io_config.spi_mode = 2;
        io_config.pclk_hz = 40 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &panel_io));

        ESP_LOGD(TAG, "Install LCD driver");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR;
        panel_config.bits_per_pixel = 16;
        ESP_ERROR_CHECK(esp_lcd_new_panel_ili9341(panel_io, &panel_config, &panel));
        
        esp_lcd_panel_reset(panel);
        aw9523_->ResetIli9342();

        esp_lcd_panel_init(panel);
        esp_lcd_panel_invert_color(panel, true);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);

        display_ = new SpiLcdDisplay(panel_io, panel,
                                    DISPLAY_WIDTH, DISPLAY_HEIGHT, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
    }

     void InitializeCamera() {
        static esp_cam_ctlr_dvp_pin_config_t dvp_pin_config = {
            .data_width = CAM_CTLR_DATA_WIDTH_8,
            .data_io = {
                [0] = CAMERA_PIN_D0,
                [1] = CAMERA_PIN_D1,
                [2] = CAMERA_PIN_D2,
                [3] = CAMERA_PIN_D3,
                [4] = CAMERA_PIN_D4,
                [5] = CAMERA_PIN_D5,
                [6] = CAMERA_PIN_D6,
                [7] = CAMERA_PIN_D7,
            },
            .vsync_io = CAMERA_PIN_VSYNC,
            .de_io = CAMERA_PIN_HREF,
            .pclk_io = CAMERA_PIN_PCLK,
            .xclk_io = CAMERA_PIN_XCLK,
        };

        esp_video_init_sccb_config_t sccb_config = {
            .init_sccb = false,
            .i2c_handle = i2c_bus_,
            .freq = 100000,
        };

        esp_video_init_dvp_config_t dvp_config = {
            .sccb_config = sccb_config,
            .reset_pin = CAMERA_PIN_RESET,
            .pwdn_pin = CAMERA_PIN_PWDN,
            .dvp_pin = dvp_pin_config,
            .xclk_freq = XCLK_FREQ_HZ,
        };

        esp_video_init_config_t video_config = {
            .dvp = &dvp_config,
        };

        camera_ = new EspVideo(video_config);
        camera_->SetHMirror(false);
    }

    bool servo_ok_ = false;
    bool rgb_ok_ = false;
    static constexpr uint8_t RGB_LED_COUNT = 12;  // StackChan base has 12 WS2812C
    static constexpr uint8_t RGB_DATA_PIN  = 13;  // PY32 expander pin (not ESP32 GPIO)

    void InitializeIOExpander() {
        ESP_LOGI(TAG, "Init PY32 IO expander (I2C addr 0x%02X)", Py32IoExpander::DEFAULT_ADDR);
        io_expander_ = std::unique_ptr<Py32IoExpander>(new Py32IoExpander(i2c_bus_));

        // PY32 boots slowly and is unreliable in the first few hundred ms
        // after power-on. Retry the probe up to 5 times with 500ms gaps —
        // total budget ~2.5 s, which dominates boot latency by maybe 1.5 s
        // in the worst case but is still well under the time spent on
        // I2C scan + LCD panel init that happen earlier.
        constexpr int kBeginRetries  = 5;
        constexpr int kBeginDelayMs  = 500;
        bool   ok = false;
        uint8_t version = 0;
        int     winning_attempt = 0;
        for (int i = 0; i < kBeginRetries; i++) {
            vTaskDelay(pdMS_TO_TICKS(kBeginDelayMs));
            if (io_expander_->Begin(&version)) {
                ok = true;
                winning_attempt = i + 1;
                break;
            }
            ESP_LOGW(TAG, "PY32 not responding, retry %d/%d", i + 1, kBeginRetries);
        }

        if (!ok) {
            ESP_LOGE(TAG, "PY32 IO expander FAILED after %d attempts; servo will be POWERLESS",
                     kBeginRetries);
            io_expander_.reset();
            return;
        }
        ESP_LOGI(TAG, "PY32 IO expander READY (version=0x%02X, attempt=%d/%d)",
                 version, winning_attempt, kBeginRetries);

        // Pin 0 = VM EN (servo power switch). Output, pull-up, drive HIGH.
        // We track each step so a partial success is reported precisely
        // (e.g. direction set but pull-up failed) — much easier to debug
        // than the previous "all-void, hope it stuck" version.
        bool ok_dir   = io_expander_->SetDirection(0, true);
        bool ok_pull  = io_expander_->SetPullMode(0, true);
        bool ok_write = io_expander_->DigitalWrite(0, true);
        vTaskDelay(pdMS_TO_TICKS(200));

        if (!ok_dir || !ok_pull || !ok_write) {
            const char* failed = "?";
            if (!ok_dir)        failed = "SetDirection";
            else if (!ok_pull)  failed = "SetPullMode";
            else if (!ok_write) failed = "DigitalWrite";
            ESP_LOGE(TAG, "Servo power ENABLE FAILED at step=%s", failed);
            return;
        }

        // Verify by reading back the output low-byte register. Bit 0 must
        // be high. If not, the chip ACK'd but the level didn't latch — log
        // it loudly so we know the next move_head will be silent.
        uint8_t out_low = 0;
        if (io_expander_->ReadOutputLow(&out_low)) {
            if (out_low & 0x01) {
                ESP_LOGI(TAG, "Servo power ENABLED via PY32 pin 0 "
                              "(VM EN HIGH confirmed, REG_GPIO_O_L=0x%02X)", out_low);
            } else {
                ESP_LOGE(TAG, "Servo power write succeeded but readback shows "
                              "pin 0 LOW (REG_GPIO_O_L=0x%02X) — VM EN may be off!",
                              out_low);
            }
        } else {
            // Read failed but writes succeeded; assume the writes took.
            ESP_LOGW(TAG, "Servo power writes OK, but readback verify failed "
                          "(can't confirm VM EN level)");
        }

        // ---- RGB strip init (12x WS2812C on the StackChan base) ----
        // The data line is on PY32 pin 13 (not an ESP32 GPIO); the PY32
        // bit-bangs the WS2812 protocol itself. We just write RGB565 into
        // its LED RAM and toggle the latch bit. Sequence is the same as the
        // M5 BSP: configure pin 13 as push-pull output with pull-up,
        // SetLedCount(12), small settle delay, then clear all LEDs.
        bool ok_d   = io_expander_->SetDirection(RGB_DATA_PIN, true);
        bool ok_p   = io_expander_->SetPullMode(RGB_DATA_PIN, true);
        bool ok_dr  = io_expander_->SetDriveMode(RGB_DATA_PIN, false);
        bool ok_cnt = io_expander_->SetLedCount(RGB_LED_COUNT);
        if (!ok_d || !ok_p || !ok_dr || !ok_cnt) {
            const char* failed = "?";
            if      (!ok_d)   failed = "SetDirection(13)";
            else if (!ok_p)   failed = "SetPullMode(13)";
            else if (!ok_dr) failed = "SetDriveMode(13)";
            else if (!ok_cnt) failed = "SetLedCount";
            ESP_LOGE(TAG, "RGB strip init FAILED at step=%s; LEDs disabled", failed);
            return;
        }
        // M5 reference firmware waits 200 ms after SetLedCount before the
        // first refresh — the PY32 internal LED engine needs the settle.
        vTaskDelay(pdMS_TO_TICKS(200));

        // Clear strip: zero RAM in one burst, then latch.
        uint8_t clear_buf[RGB_LED_COUNT * 2] = {0};
        bool ok_clear = io_expander_->SetLedData(clear_buf, sizeof(clear_buf));
        bool ok_ref   = io_expander_->RefreshLeds();
        if (!ok_clear || !ok_ref) {
            ESP_LOGE(TAG, "RGB strip clear FAILED (data=%d refresh=%d); LEDs disabled",
                     ok_clear, ok_ref);
            return;
        }
        rgb_ok_ = true;
        ESP_LOGI(TAG, "RGB strip READY (%d WS2812C via PY32 pin %d, all cleared)",
                 RGB_LED_COUNT, RGB_DATA_PIN);
    }

    // Helpers for the LED MCP tools below. Centralised so the parsing/
    // clamping logic isn't duplicated in three handlers.
    static uint8_t ClampByte(int v) {
        if (v < 0) return 0;
        if (v > 255) return 255;
        return (uint8_t)v;
    }

    // Pack one RGB888 sample into the {lo, hi} RGB565 pair the PY32
    // expects in its LED RAM.
    static void PackRgb565(uint8_t r, uint8_t g, uint8_t b, uint8_t out[2]) {
        uint16_t v = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
        out[0] = (uint8_t)(v & 0xFF);
        out[1] = (uint8_t)((v >> 8) & 0xFF);
    }

    void InitializeServo() {
        ESP_LOGI(TAG, "Init SCS0009 servo bus (UART%d, baud=%d, tx=%d, rx=%d)",
                 SERVO_UART_NUM, SERVO_BAUDRATE, SERVO_TX_PIN, SERVO_RX_PIN);
#if CONFIG_STACKCHAN_SERVO_FEETECH
        // FeetechScs::begin() returns void and uses ESP_ERROR_CHECK internally,
        // so a UART configuration error aborts the boot rather than reporting
        // false. If begin() returns to us, init succeeded.
        scs_bus_.begin(SERVO_UART_NUM, SERVO_BAUDRATE, SERVO_TX_PIN, SERVO_RX_PIN);
        servo_ok_ = true;
#else
        // SCServo_lib SCSCL::begin() returns bool — false if UART setup failed.
        servo_ok_ = scs_bus_.begin(SERVO_UART_NUM, SERVO_BAUDRATE, SERVO_TX_PIN, SERVO_RX_PIN);
#endif
        // ACK reading is enabled (SCS::Level defaults to 1). genWrite() will
        // wait for the SCS0009's 6-byte ACK packet before returning, which
        // implicitly enforces an inter-frame gap and prevents a follow-up
        // WritePos from colliding with a still-processing servo. This aligns
        // with the M5 StackChan official BSP behaviour (which never touches
        // Level). Was: scs_bus_.Level = 0 — turned out to silently drop
        // every WritePos after the first one ("starts moving once, then
        // never again" symptom).
        ESP_LOGI(TAG, "Servo bus init: %s (Level=1, ACK enabled)", servo_ok_ ? "OK" : "FAILED");

        if (servo_ok_) {
            motion_mutex_ = xSemaphoreCreateMutex();
            scs_bus_mutex_ = xSemaphoreCreateMutex();
            if (motion_mutex_ == nullptr || scs_bus_mutex_ == nullptr) {
                ESP_LOGE(TAG, "Failed to create servo mutexes: motion=%p scs_bus=%p; disabling servo",
                         motion_mutex_, scs_bus_mutex_);
                if (motion_mutex_ != nullptr) {
                    vSemaphoreDelete(motion_mutex_);
                    motion_mutex_ = nullptr;
                }
                if (scs_bus_mutex_ != nullptr) {
                    vSemaphoreDelete(scs_bus_mutex_);
                    scs_bus_mutex_ = nullptr;
                }
                servo_ok_ = false;
                return;
            }

#if CONFIG_STACKCHAN_SERVO_DELEGATED_MOTION
            motion_driver_ = std::make_unique<ServoDelegatedMotionDriver>(
                scs_bus_, scs_bus_mutex_, motion_mutex_,
                yaw_motion_, pitch_motion_);
#else
            motion_driver_ = std::make_unique<HostInterpolationMotionDriver>(
                scs_bus_, scs_bus_mutex_, motion_mutex_,
                yaw_motion_, pitch_motion_);
#endif
            if (!motion_driver_->Initialize()) {
                ESP_LOGE(TAG, "Failed to initialize motion driver; disabling servo");
                motion_driver_.reset();
                vSemaphoreDelete(motion_mutex_);
                motion_mutex_ = nullptr;
                vSemaphoreDelete(scs_bus_mutex_);
                scs_bus_mutex_ = nullptr;
                servo_ok_ = false;
                return;
            }

            // Issue #121 (Problem 1, "downward drop on power-on") + #123
            // (boot-init diagnostics).
            //
            // Background: the SCS0009 retains its commanded set-point
            // across power cycles (Hypothesis 1 in #121, confirmed by
            // the firmware-v1.4.1 clean-install reproduction -- after a
            // full NVS reset on the ESP32 side, the boot pre-init
            // `ReadPos` still matched the pre-power-off pose exactly,
            // demonstrating the set-point lives in the servo itself,
            // not in firmware-side NVS). When VM_EN asserts at boot,
            // the servo restores torque and snaps toward that retained
            // target before any firmware-side speed limiting can apply
            // -- audible as a mechanical end-stop impact when the
            // previous session ended near pitch=0, and visible as a
            // downward drop in general.
            //
            // The mitigation is a `WritePos(id, current_pos, time=0,
            // speed=0)` per servo, which the SCS0009 treats as a new
            // target equal to its current position. This interrupts any
            // in-progress snap motion and leaves the servo stationary
            // at the raw position the immediately-preceding `ReadPos`
            // observed, until the subsequent interpolating boot-init
            // climb begins.
            //
            // Efficacy depends on the `ReadPos` + `WritePos` pair
            // completing while the servo is still mid-snap. To minimize
            // that window, pitch (the only axis that exhibits the
            // downward drop) is read AND held BEFORE the yaw axis is
            // touched on the SCS0009 bus -- any yaw `ReadPos` /
            // `WritePos` / ACK wait interposed between the pitch
            // `ReadPos` and pitch `WritePos` would widen the window
            // and risk the snap completing into an end-stop before the
            // pitch hold reaches the servo. The unified "Boot pre-init
            // ReadPos" diagnostic line (#123) is emitted after both
            // holds because it is purely informational and not on the
            // timing-critical path. If a `ReadPos` value lands close
            // to a previous session's commanded target (e.g. raw pitch
            // near 620 for pitch=0 deg), the snap was likely still in
            // progress and the hold is expected to truncate it; if a
            // `ReadPos` value is already at an end-stop or fails, the
            // hold for that boot is a no-op or skipped and a deeper
            // fix (e.g. firmware-controlled VM_EN sequencing through
            // the PY32 IO-expander) would be required, tracked
            // separately.

            // Phase 1a: pitch first -- read and immediately hold.
            //
            // Issue #138: retry ReadPos to absorb the SCS0009 ~200 ms
            // startup latency observed after VM_EN HIGH on the PMIC
            // long-press OFF/ON path. Without retry, the first ReadPos
            // (measured at around tick 140 on this hardware) typically
            // lands inside the wake-up window and returns -1, causing
            // the snap-suppress hold below to skip on exactly the path
            // #121 Problem 1 targets. Budget: 5 attempts × 50 ms = 250 ms
            // total per axis, well above the observed ~200 ms latency.
            // This is distinct from the SCS0009 bus hang (#100), which a
            // fixed retry budget cannot clear; in that case all attempts
            // fail and the safe-fallback branch in Phase 2 below seeds
            // pitch_motion_.current_deg with BOOT_INIT_PITCH_DEG to avoid
            // an end-stop walk during the subsequent boot-init
            // `WriteHeadAngles` interpolation.
            // Loop form mirrors the `get_head_angles` MCP-tool retry below
            // (set attempts to `i + 1` inside the loop so the final value
            // equals the number of attempts actually made, even when all
            // retries fail). The previous `for (attempts = 1; attempts <=
            // MAX; ++attempts)` form left attempts at MAX+1 on failure
            // and made the diagnostic log overstate the attempt count.
            constexpr int BOOT_READPOS_MAX_ATTEMPTS = 5;
            constexpr uint32_t BOOT_READPOS_RETRY_MS = 50;
            int pitch_pos_actual = -1;
            int pitch_attempts = 0;
            for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
                pitch_attempts = i + 1;
                pitch_pos_actual = scs_bus_.ReadPos(SERVO_PITCH_ID);
                if (pitch_pos_actual >= 0) break;
                if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                    vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
                }
            }
            if (pitch_pos_actual >= 0) {
                // Bound the snap-suppress hold to the SAFE_PITCH_MIN..
                // SAFE_PITCH_MAX range applied at every other pitch
                // servo-write boundary in this file (see PitchDegToPos
                // and the Phase 2 restored_pitch clamp below). If the
                // boot `ReadPos` lands outside that range -- for example
                // because the servo bus came back online holding a
                // previous session's out-of-range set-point, or the
                // head was hand-pushed beyond an end-stop -- writing
                // the raw position back would bypass that safety
                // boundary and pin the servo against the stall current
                // it is held there from. In that case skip the hold
                // and let the subsequent interpolating boot-init climb
                // to (yaw=0, pitch=45) drive the head back into the
                // safe range through the existing speed-limited path.
                constexpr int PITCH_SAFE_RAW_MIN =
                    620 + SAFE_PITCH_MIN * 16 / 5;  // raw 620 at deg=0
                constexpr int PITCH_SAFE_RAW_MAX =
                    620 + SAFE_PITCH_MAX * 16 / 5;  // raw 901 at deg=88
                if (pitch_pos_actual >= PITCH_SAFE_RAW_MIN &&
                    pitch_pos_actual <= PITCH_SAFE_RAW_MAX) {
                    int pitch_hold_r = scs_bus_.WritePos(
                        SERVO_PITCH_ID, pitch_pos_actual, 0, 0);
                    ESP_LOGI(TAG,
                             "Boot snap-suppress pitch hold(pos=%d): r=%d",
                             pitch_pos_actual, pitch_hold_r);
                } else {
                    ESP_LOGW(TAG,
                             "Boot snap-suppress pitch skipped: ReadPos=%d outside safe raw range [%d, %d]; relying on boot-init climb",
                             pitch_pos_actual,
                             PITCH_SAFE_RAW_MIN, PITCH_SAFE_RAW_MAX);
                }
            }

            // Phase 1b: yaw second -- no analogous snap-into-end-stop
            // failure mode, so timing is not critical. Retry budget
            // matches pitch (Issue #138) for symmetry; in practice yaw
            // typically succeeds on the first attempt because the pitch
            // retries above have already consumed the SCS0009 startup-
            // latency window on the shared bus.
            int yaw_pos_actual = -1;
            int yaw_attempts = 0;
            for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
                yaw_attempts = i + 1;
                yaw_pos_actual = scs_bus_.ReadPos(SERVO_YAW_ID);
                if (yaw_pos_actual >= 0) break;
                if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                    vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
                }
            }
            if (yaw_pos_actual >= 0) {
                int yaw_hold_r = scs_bus_.WritePos(
                    SERVO_YAW_ID, yaw_pos_actual, 0, 0);
                ESP_LOGI(TAG,
                         "Boot snap-suppress yaw hold(pos=%d): r=%d",
                         yaw_pos_actual, yaw_hold_r);
            }

            // Phase 1c (diagnostic, #123): unified pre-init ReadPos log
            // with tick timestamp. Off the timing-critical path
            // intentionally; ServoTask has not been created yet, so no
            // `scs_bus_mutex_` contention is possible at this point.
            ESP_LOGI(TAG,
                     "Boot pre-init ReadPos: yaw_raw=%d (attempts=%d) "
                     "pitch_raw=%d (attempts=%d) tick=%u",
                     yaw_pos_actual, yaw_attempts,
                     pitch_pos_actual, pitch_attempts,
                     (unsigned)xTaskGetTickCount());

            // Phase 2: software-side current_deg restore. Order does not
            // affect the SCS0009 bus -- these only update firmware-side
            // motion state for the upcoming interpolating boot-init
            // climb.
            if (yaw_pos_actual >= 0) {
                yaw_motion_.current_deg = (yaw_pos_actual - 460) * 5 / 16;
                ESP_LOGI(TAG, "Restored yaw_motion_.current_deg=%d from ReadPos=%d",
                         yaw_motion_.current_deg, yaw_pos_actual);
            } else {
                // Issue #138: seed yaw current_deg to BOOT_INIT_YAW_DEG
                // rather than leaving the struct default. The default
                // happens to be 0 (== BOOT_INIT_YAW_DEG today), so this
                // is a no-op assignment in current numerical terms; the
                // explicit form keeps intent visible and propagates any
                // future change to BOOT_INIT_YAW_DEG.
                yaw_motion_.current_deg = BOOT_INIT_YAW_DEG;
                ESP_LOGW(TAG,
                         "Failed to ReadPos(yaw) after %d attempts; "
                         "seeded current_deg=%d (BOOT_INIT_YAW_DEG)",
                         BOOT_READPOS_MAX_ATTEMPTS, BOOT_INIT_YAW_DEG);
            }
            if (pitch_pos_actual >= 0) {
                int restored_pitch = (pitch_pos_actual - 620) * 5 / 16;
                // Issue #80: if the device booted with the head physically
                // pushed below the safe range (e.g. previous unsafe firmware
                // or manual handling), don't carry that negative starting
                // angle into motion interpolation — clamp before storing so
                // subsequent interpolation runs only over safe positions.
                if (restored_pitch < SAFE_PITCH_MIN) restored_pitch = SAFE_PITCH_MIN;
                if (restored_pitch > SAFE_PITCH_MAX) restored_pitch = SAFE_PITCH_MAX;
                pitch_motion_.current_deg = restored_pitch;
                ESP_LOGI(TAG, "Restored pitch_motion_.current_deg=%d from ReadPos=%d (clamped to safe range %d..%d)",
                         pitch_motion_.current_deg, pitch_pos_actual, SAFE_PITCH_MIN, SAFE_PITCH_MAX);
            } else {
                // Issue #138: seed pitch current_deg to BOOT_INIT_PITCH_DEG.
                // Without this, the boot-init `WriteHeadAngles(0, 45, 4000)`
                // interpolation below would start from the struct-default
                // `current_deg=0` (== pos=620 at deg=0, the lower mechanical
                // end-stop) and walk WritePos calls upward (pos=620, 623,
                // 626, ...) through end-stop-adjacent positions before
                // reaching the target -- risking servo bus degradation if
                // the SCS0009 wakes up mid-sequence. Seeding to
                // BOOT_INIT_PITCH_DEG makes the subsequent interpolation a
                // near-no-op (start_deg == target_deg == 45) which keeps
                // the servo away from end-stop territory throughout the
                // wake-up window. This is the firmware-side counterpart to
                // the Phase 1a ReadPos retry: retry absorbs the typical
                // wake-up case so the snap-suppress hold can fire; this
                // seed handles the residual case where wake-up exceeds the
                // retry budget or the bus is genuinely hung (#100).
                pitch_motion_.current_deg = BOOT_INIT_PITCH_DEG;
                ESP_LOGW(TAG,
                         "Failed to ReadPos(pitch) after %d attempts; "
                         "seeded current_deg=%d (BOOT_INIT_PITCH_DEG) "
                         "to avoid end-stop walk during boot-init climb",
                         BOOT_READPOS_MAX_ATTEMPTS, BOOT_INIT_PITCH_DEG);
            }

            BaseType_t ok = xTaskCreate(&StackChanBoard::ServoTaskTrampoline,
                                        "servo_motion", 4096, this, 5,
                                        &servo_task_handle_);
            if (ok != pdPASS) {
                ESP_LOGE(TAG, "Failed to create servo_motion task; disabling servo");
                if (motion_mutex_ != nullptr) {
                    vSemaphoreDelete(motion_mutex_);
                    motion_mutex_ = nullptr;
                }
                if (scs_bus_mutex_ != nullptr) {
                    vSemaphoreDelete(scs_bus_mutex_);
                    scs_bus_mutex_ = nullptr;
                }
                motion_driver_.reset();
                servo_task_handle_ = nullptr;
                servo_ok_ = false;
                return;
            }

            // Issue #115: boot-time initialization to a fall-safe neutral
            // pose. Without this, the head retains whatever angle it was
            // left at on power-down — including end-stop positions (e.g.
            // pitch=0) that trigger the SCS0009 bus-hang documented in
            // #100 on the very first user-driven motion. By the time any
            // MCP command can arrive, we want the head already moved to
            // the center of the M5Stack-recommended 5..85° pitch range,
            // well clear of both mechanical end-stops.
            //
            // Design follows the goHome() pattern in m5stack/StackChan
            // (apps/app_setup/workers/servo.cpp:144) where the setup
            // app calls motion.goHome(speed) at boot, and the 1-second
            // positioning timing established in mongonta0716/stackchan-
            // arduino attachServos(). 1000ms move via the existing
            // interpolating WriteHeadAngles path keeps frame-rate stall
            // currents low; the extra 100ms vTaskDelay covers servo
            // settling before any subsequent motion can arrive.
            //
            // Implements #99 Option C and the boot-init aspect of #100
            // direction E. Existing pitch guards (#80 / #98 / #109)
            // continue to apply unchanged.
            // BOOT_INIT_YAW_DEG / BOOT_INIT_PITCH_DEG / BOOT_INIT_MOVE_MS
            // are class-level static constexpr; see the comment block at
            // their declaration above the YawDegToPos helper for the full
            // rationale (Issue #115 target pose, Issue #121 Problem 2
            // slower climb, Issue #138 promotion to class scope for the
            // safe-fallback seed in Phase 2 above).
            // Compute Phase 0 duration from the current_deg → target
            // deltas at BOOT_INIT_TARGET_DEG_PER_SEC, floored at
            // BOOT_INIT_MOVE_MS to keep the SCS0009 wake-up latency
            // window covered on the PMIC OFF/ON path (where Phase 0
            // is a no-op of effect but the BOOT_INIT_MOVE_MS budget
            // still needs to elapse before Phase 0' ReadPos). Without
            // this calculation, a yaw-90° (or any large pre-power-off
            // angle) prior set-point would run the boot-init yaw
            // motion at 30 deg/s+, exceeding the 15 deg/s cap.
            int phase0_yaw_delta;
            int phase0_pitch_delta;
            phase0_yaw_delta =
                BOOT_INIT_YAW_DEG - static_cast<int>(motion_driver_->GetYawDeg());
            phase0_pitch_delta =
                BOOT_INIT_PITCH_DEG - static_cast<int>(motion_driver_->GetPitchDeg());
            if (phase0_yaw_delta < 0) phase0_yaw_delta = -phase0_yaw_delta;
            if (phase0_pitch_delta < 0) phase0_pitch_delta = -phase0_pitch_delta;
            int phase0_max_delta = phase0_yaw_delta > phase0_pitch_delta
                ? phase0_yaw_delta : phase0_pitch_delta;
            uint32_t phase0_duration_ms =
                (uint32_t)phase0_max_delta * 1000U /
                BOOT_INIT_TARGET_DEG_PER_SEC;
            if (phase0_duration_ms < BOOT_INIT_MOVE_MS) {
                phase0_duration_ms = BOOT_INIT_MOVE_MS;
            }
            TickType_t boot_init_start_tick = xTaskGetTickCount();
            WriteHeadAngles(BOOT_INIT_YAW_DEG, BOOT_INIT_PITCH_DEG,
                            phase0_duration_ms,
                            /* prefer_linear = */ true);
            // Two-phase boot-init wait:
            //
            // (1) Mandatory minimum: vTaskDelay until
            //     phase0_duration_ms + 100 ms has elapsed. This must
            //     run UNCONDITIONALLY — independent of IsMoving() —
            //     because phase0_duration_ms is floored to
            //     BOOT_INIT_MOVE_MS specifically to cover the
            //     SCS0009 wake-up window on the PMIC OFF/ON path,
            //     even when WriteHeadAngles is a no-op (Phase 2
            //     safe-fallback seeded current_deg to exactly the
            //     boot target → motion_driver_->IsMoving() == false
            //     immediately → Phase 0' ReadPos would otherwise run
            //     inside the wake-up latency window and exhaust).
            //
            // (2) Optional extension: while motion_driver_->IsMoving()
            //     reports a motion still in flight, keep waiting up
            //     to a safety deadline. This covers the ServoDelegated
            //     path where the actual servo motion can start late
            //     due to dispatch retry latency (max 5 ×
            //     MOTION_POLL_INTERVAL_MS = 250 ms) — phase (1)'s
            //     budget may finish before the servo has completed
            //     its internal interpolation in that worst case.
            TickType_t boot_init_min_deadline =
                xTaskGetTickCount() +
                pdMS_TO_TICKS(phase0_duration_ms + 100);
            // Safety extension covers the worst-case ServoDelegated
            // dispatch latency. Each retry round dispatches yaw and
            // pitch sequentially under scs_bus_mutex_, so one fully-
            // timing-out round on both axes costs approximately:
            //   MOTION_POLL_INTERVAL_MS (50 ms tick wake)
            // + yaw WritePos ACK timeout (~100 ms; SCSCL's
            //   SCSerial::IOTimeOut = 100 ms, FeetechScs comparable)
            // + kInterFrameGap (10 ms)
            // + pitch WritePos ACK timeout (~100 ms)
            // ≈ 260 ms per retry round.
            //
            // 4 failing rounds + 1 successful round bound the
            // worst-case dispatch latency at roughly 4 × 260 ≈ 1040 ms.
            // Add a settle margin so the wait outlasts a genuine
            // delayed dispatch instead of breaking while IsMoving()
            // is legitimately true. 2000 ms covers the full retry
            // budget plus margin.
            //
            // HostInterpolation path is unaffected (dispatch latency
            // is 0 there; the wait exits well before this deadline
            // regardless).
            TickType_t boot_init_safety_deadline =
                boot_init_min_deadline + pdMS_TO_TICKS(2000);
            while ((int32_t)(xTaskGetTickCount() -
                             boot_init_min_deadline) < 0) {
                vTaskDelay(pdMS_TO_TICKS(50));
            }
            while (motion_driver_->IsMoving()) {
                if ((int32_t)(xTaskGetTickCount() -
                              boot_init_safety_deadline) >= 0) {
                    ESP_LOGW(TAG,
                             "Boot init Phase 0 wait safety deadline elapsed while motion_driver_ still reports moving; proceeding to Phase 0' ReadPos anyway");
                    break;
                }
                vTaskDelay(pdMS_TO_TICKS(50));
            }

            // Issue #123: capture post-init ReadPos so the boot-init effect
            // is observable in the serial log. ServoTask is now running, so
            // hold scs_bus_mutex_ across the ReadPos pair.
            //
            // Retry budget mirrors Phase 1a / 1b and the get_head_angles
            // MCP-tool retry. A transient `ReadPos == -1` on a healthy
            // servo would otherwise cause Phase 0' re-sync to skip,
            // leaving current_deg slightly stale for the session.
            int post_yaw_pos = -1;
            int post_pitch_pos = -1;
            int post_yaw_attempts = 0;
            int post_pitch_attempts = 0;
            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
            for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
                post_yaw_attempts = i + 1;
                post_yaw_pos = scs_bus_.ReadPos(SERVO_YAW_ID);
                if (post_yaw_pos >= 0) break;
                if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                    vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
                }
            }
            for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
                post_pitch_attempts = i + 1;
                post_pitch_pos = scs_bus_.ReadPos(SERVO_PITCH_ID);
                if (post_pitch_pos >= 0) break;
                if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                    vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
                }
            }
            xSemaphoreGive(scs_bus_mutex_);
            TickType_t boot_init_end_tick = xTaskGetTickCount();
            ESP_LOGI(TAG,
                     "Boot-time servo init complete: target yaw=%d pitch=%d "
                     "(move=%ums), post-ReadPos: yaw_raw=%d (attempts=%d) "
                     "pitch_raw=%d (attempts=%d), elapsed_ms=%u",
                     BOOT_INIT_YAW_DEG, BOOT_INIT_PITCH_DEG,
                     (unsigned)phase0_duration_ms,
                     post_yaw_pos, post_yaw_attempts,
                     post_pitch_pos, post_pitch_attempts,
                     (unsigned)((boot_init_end_tick - boot_init_start_tick) *
                                portTICK_PERIOD_MS));

            // Phase 0': mandatory current_deg re-sync from post-init
            // ReadPos before boot-time servo initialization completes.
            //
            // Background: on the PMIC long-press OFF / ON path, Phase 1
            // ReadPos retries can exhaust the budget while the SCS0009
            // is still in its wake-up latency window; Phase 2 then
            // seeds current_deg with BOOT_INIT_*_DEG so the Phase 0
            // interpolation is a no-op of effect. By the time the
            // BOOT_INIT_MOVE_MS-long vTaskDelay above has elapsed,
            // the SCS0009 has been powered for several seconds and a
            // ReadPos here is almost certain to succeed (the
            // observed boot log shows yaw_raw / pitch_raw populated
            // at tick ≥ ~6 s).
            //
            // Re-syncing current_deg with the actual physical position
            // now keeps the next move_head interpolation anchored to
            // where the servo really is, rather than to the Phase 2
            // safe-fallback seed.
            //
            // If a post-init ReadPos still fails, leave current_deg as
            // restored-or-seeded by Phase 2. That is safer than issuing
            // additional boot-time WritePos commands on a degraded bus.
            if (post_yaw_pos >= 0) {
                int actual_yaw_deg = (post_yaw_pos - 460) * 5 / 16;
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                if (yaw_motion_.current_deg != actual_yaw_deg) {
                    int prev_yaw_deg = yaw_motion_.current_deg;
                    yaw_motion_.current_deg = actual_yaw_deg;
                    yaw_motion_.start_deg = actual_yaw_deg;
                    yaw_motion_.target_deg = actual_yaw_deg;
                    yaw_motion_.moving = false;
                    yaw_motion_.move_start_ms =
                        (uint32_t)(boot_init_end_tick * portTICK_PERIOD_MS);
                    // Bump the driver's request_token so any Tick()
                    // snapshot taken before this re-sync no longer
                    // passes the post-bus freshness guard and does
                    // not overwrite the just-re-synced state. The
                    // delegated driver also clears its per-axis
                    // private cancellation state under the same
                    // motion_mutex_ hold.
                    motion_driver_->InvalidateAxisToken(SERVO_YAW_ID);
                    xSemaphoreGive(motion_mutex_);
                    ESP_LOGI(TAG,
                             "Phase 0' yaw re-sync: current_deg %d -> %d "
                             "(actual ReadPos=%d)",
                             prev_yaw_deg, actual_yaw_deg, post_yaw_pos);
                } else {
                    xSemaphoreGive(motion_mutex_);
                }
            } else {
                // Phase 0' ReadPos failed: current_deg holds the
                // Phase 2 restored-or-seeded value, which has NOT
                // been physically verified. Mark the axis position
                // unknown so the ServoDelegated path's no-op gate
                // does not silently skip a same-target recovery
                // command. The HostInterpolation path ignores this
                // flag; on that path the next WriteHeadAngles still
                // dispatches a fresh interpolation as before.
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                yaw_motion_.position_unknown = true;
                // Token/state invalidation covers the position_unknown
                // mutation using the same external-reset boundary as
                // the success branch above.
                motion_driver_->InvalidateAxisToken(SERVO_YAW_ID);
                xSemaphoreGive(motion_mutex_);
                ESP_LOGW(TAG,
                         "Phase 0' yaw re-sync skipped: ReadPos failed; "
                         "leaving current_deg at restored-or-seeded value "
                         "and marking position unknown for delegated no-op gate");
            }
            if (post_pitch_pos >= 0) {
                int actual_pitch_deg = (post_pitch_pos - 620) * 5 / 16;
                if (actual_pitch_deg < SAFE_PITCH_MIN) actual_pitch_deg = SAFE_PITCH_MIN;
                if (actual_pitch_deg > SAFE_PITCH_MAX) actual_pitch_deg = SAFE_PITCH_MAX;
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                if (pitch_motion_.current_deg != actual_pitch_deg) {
                    int prev_pitch_deg = pitch_motion_.current_deg;
                    pitch_motion_.current_deg = actual_pitch_deg;
                    pitch_motion_.start_deg = actual_pitch_deg;
                    pitch_motion_.target_deg = actual_pitch_deg;
                    pitch_motion_.moving = false;
                    pitch_motion_.move_start_ms =
                        (uint32_t)(boot_init_end_tick * portTICK_PERIOD_MS);
                    // Invalidate the driver's token/state boundary
                    // (see the yaw branch above for the rationale).
                    motion_driver_->InvalidateAxisToken(SERVO_PITCH_ID);
                    xSemaphoreGive(motion_mutex_);
                    ESP_LOGI(TAG,
                             "Phase 0' pitch re-sync: current_deg %d -> %d "
                             "(actual ReadPos=%d, clamped to safe range %d..%d)",
                             prev_pitch_deg, actual_pitch_deg, post_pitch_pos,
                             SAFE_PITCH_MIN, SAFE_PITCH_MAX);
                } else {
                    xSemaphoreGive(motion_mutex_);
                }
            } else {
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                pitch_motion_.position_unknown = true;
                // Invalidate the driver's token/state boundary for the
                // position_unknown mutation (see the yaw fail branch
                // above).
                motion_driver_->InvalidateAxisToken(SERVO_PITCH_ID);
                xSemaphoreGive(motion_mutex_);
                ESP_LOGW(TAG,
                         "Phase 0' pitch re-sync skipped: ReadPos failed; "
                         "leaving current_deg at restored-or-seeded value "
                         "and marking position unknown for delegated no-op gate");
            }
            boot_init_done_.store(true, std::memory_order_release);
        }
    }

    ServoTorqueResult InternalSetServoTorque(bool yaw_enabled,
                                             bool pitch_enabled,
                                             ReleaseReason reason,
                                             uint32_t expected_release_epoch =
                                                 0) {
        ServoTorqueResult result;
        const bool disables_axis = !yaw_enabled || !pitch_enabled;

        auto update_bus_ok = [&]() {
#if CONFIG_STACKCHAN_SERVO_FEETECH
            result.yaw_ok = (result.yaw_bus_return == 0);
            result.pitch_ok = (result.pitch_bus_return == 0);
#else
            result.yaw_ok = (result.yaw_bus_return > 0);
            result.pitch_ok = (result.pitch_bus_return > 0);
#endif
        };

        auto log_result = [&](bool short_circuited) {
            result.short_circuited = short_circuited;
            ESP_LOGI(TAG,
                     "set_servo_torque (reason=%s): servo_ok=%d "
                     "yaw_enabled=%d (r=%d) pitch_enabled=%d (r=%d) "
                     "short_circuited=%d",
                     ReleaseReasonName(reason),
                     servo_ok_ ? 1 : 0,
                     yaw_enabled ? 1 : 0, result.yaw_bus_return,
                     pitch_enabled ? 1 : 0, result.pitch_bus_return,
                     short_circuited ? 1 : 0);
        };

        auto finish = [&](bool short_circuited) -> ServoTorqueResult {
            log_result(short_circuited);
            return result;
        };

        if (!servo_ok_ || scs_bus_mutex_ == nullptr) {
            return finish(false);
        }

        // Fully-symmetric re-engage remains bus-ordered even when it becomes
        // a no-op. If an auto-idle OFF has already published kReleasing, the
        // manual path waits for that pending transition before entering the
        // bus section.
        if (yaw_enabled && pitch_enabled) {
            // Pre-mutex wait: this reduces obvious contention before
            // attempting scs_bus_mutex_. The post-mutex re-check below closes
            // the pre-check-to-mutex TOCTOU window.
            if (reason == ReleaseReason::kManual) {
                auto state_pre =
                    torque_state_.load(std::memory_order_acquire);
                if (state_pre == TorqueState::kReleasing) {
                    if (!WaitForKReleasingToClear()) {
                        ESP_LOGW(TAG,
                                 "set_servo_torque (reason=%s): kReleasing "
                                 "not clearing within wait budget; skipping "
                                 "bus frames, caller may retry.",
                                 ReleaseReasonName(reason));
                        log_result(true);
                        return result;
                    }
                    // After the wait, state may be kEngaged (OFF failed and
                    // rolled back) or kReleased (OFF succeeded). Fall through
                    // to the existing (true, true) logic, which short-circuits
                    // on kEngaged and proceeds normally on kReleased/kPartial.
                }
            }

            // Bounded retry for the remaining TOCTOU window: torque_state_
            // can flip to kReleasing between the pre-mutex check and
            // xSemaphoreTake(). Once the bus mutex is held, re-check; if an
            // auto-release OFF was published in that gap, release the mutex,
            // wait, and retry. kReengagement is exempt because
            // EnsureTorqueEngagedBeforeMove() already waited; kAutoIdle does
            // not enter this (true, true) branch.
            for (int attempt = 0; attempt < kMaxManualReengageRetries;
                 ++attempt) {
                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);

                if (reason == ReleaseReason::kManual &&
                    torque_state_.load(std::memory_order_acquire) ==
                        TorqueState::kReleasing) {
                    xSemaphoreGive(scs_bus_mutex_);
                    if (!WaitForKReleasingToClear()) {
                        ESP_LOGW(TAG,
                                 "set_servo_torque (reason=%s): kReleasing "
                                 "persists after attempt %d; skipping bus "
                                 "frames, caller may retry.",
                                 ReleaseReasonName(reason),
                                 attempt);
                        log_result(true);
                        return result;
                    }
                    continue;
                }

                if (torque_state_.load(std::memory_order_acquire) ==
                    TorqueState::kEngaged) {
                    log_result(true);
                    xSemaphoreGive(scs_bus_mutex_);
                    return result;
                }
                result.yaw_bus_return =
                    scs_bus_.EnableTorque(SERVO_YAW_ID, 1);
                result.pitch_bus_return =
                    scs_bus_.EnableTorque(SERVO_PITCH_ID, 1);
                update_bus_ok();
                if (result.yaw_ok) {
                    yaw_torque_enabled_ = true;
                }
                if (result.pitch_ok) {
                    pitch_torque_enabled_ = true;
                }
                PublishTorqueState();
                log_result(false);
                xSemaphoreGive(scs_bus_mutex_);
                return result;
            }

            ESP_LOGW(TAG,
                     "set_servo_torque (reason=%s): kReleasing observed in "
                     "all %d post-mutex retries; skipping bus frames.",
                     ReleaseReasonName(reason),
                     kMaxManualReengageRetries);
            log_result(true);
            return result;
        } else {
            if (!yaw_enabled && !pitch_enabled) {
                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                bool already_released =
                    torque_state_.load(std::memory_order_acquire) ==
                    TorqueState::kReleased;
                if (already_released) {
                    log_result(true);
                    xSemaphoreGive(scs_bus_mutex_);
                    return result;
                }
                xSemaphoreGive(scs_bus_mutex_);
            }

            // Preserve the existing cancellation-first disable path exactly:
            // reset MotionDriver state for axes being disabled before the
            // EnableTorque(OFF) bus frames can race with ServoTask writes.
            if (motion_driver_ != nullptr && motion_mutex_ != nullptr &&
                disables_axis) {
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                if (reason == ReleaseReason::kAutoIdle &&
                    (yaw_motion_.moving || pitch_motion_.moving ||
                     servo_wobble_active_.load(std::memory_order_acquire))) {
                    xSemaphoreGive(motion_mutex_);
                    return finish(true);
                }
                if (!yaw_enabled) {
                    yaw_motion_.moving = false;
                    yaw_motion_.position_unknown = true;
                    motion_driver_->InvalidateAxisToken(SERVO_YAW_ID);
                }
                if (!pitch_enabled) {
                    pitch_motion_.moving = false;
                    pitch_motion_.position_unknown = true;
                    motion_driver_->InvalidateAxisToken(SERVO_PITCH_ID);
                }
                servo_wobble_active_.store(false, std::memory_order_release);
                servo_wobble_step_.store(0, std::memory_order_release);
                // Mark the fully-OFF transition before releasing
                // motion_mutex_ so concurrent motion entries do not observe
                // the old engaged state while the OFF bus write is pending.
                if (!yaw_enabled && !pitch_enabled) {
                    (void)MarkReleasing();
                }
                xSemaphoreGive(motion_mutex_);
            }

            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
            if (reason == ReleaseReason::kAutoIdle &&
                expected_release_epoch != 0) {
                uint32_t current_epoch =
                    torque_release_epoch_.load(std::memory_order_acquire);
                auto current_state =
                    torque_state_.load(std::memory_order_acquire);
                if (current_epoch != expected_release_epoch ||
                    current_state != TorqueState::kReleasing) {
                    ESP_LOGW(TAG,
                             "auto-release OFF aborted at bus check: epoch=%u "
                             "(expected %u), state=%d (expected kReleasing); "
                             "skipping EnableTorque(0,0) frames.",
                             (unsigned)current_epoch,
                             (unsigned)expected_release_epoch,
                             (int)current_state);
                    xSemaphoreGive(scs_bus_mutex_);
                    log_result(true);
                    return result;
                }
            }
            result.yaw_bus_return = scs_bus_.EnableTorque(
                SERVO_YAW_ID, yaw_enabled ? 1 : 0);
            result.pitch_bus_return = scs_bus_.EnableTorque(
                SERVO_PITCH_ID, pitch_enabled ? 1 : 0);
            update_bus_ok();
            if (result.yaw_ok) {
                yaw_torque_enabled_ = yaw_enabled;
            }
            if (result.pitch_ok) {
                pitch_torque_enabled_ = pitch_enabled;
            }
            PublishTorqueState();
            log_result(false);
            xSemaphoreGive(scs_bus_mutex_);
            return result;
        }
    }

    void EnsureTorqueEngagedBeforeMove() {
        auto state = torque_state_.load(std::memory_order_acquire);
        if (state == TorqueState::kEngaged) {
            return;
        }
        if (!servo_ok_) {
            return;
        }

        if (state == TorqueState::kReleasing) {
            if (!WaitForKReleasingToClear()) {
                ESP_LOGW(TAG,
                         "EnsureTorqueEngagedBeforeMove: kReleasing not "
                         "clearing within wait budget, deferring to caller "
                         "retry.");
                return;
            }
            state = torque_state_.load(std::memory_order_acquire);
            if (state == TorqueState::kEngaged) {
                return;  // OFF rolled back to kEngaged
            }
        }

        // state is kPartial or kReleased -- safe to re-engage now.
        InternalSetServoTorque(true, true, ReleaseReason::kReengagement);
    }

    bool TakeMotionMutexAfterTorqueEngaged() {
        for (int attempt = 0; attempt < kMaxReengageRetries; ++attempt) {
            EnsureTorqueEngagedBeforeMove();
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            if (torque_state_.load(std::memory_order_acquire) ==
                TorqueState::kEngaged) {
                return true;
            }
            xSemaphoreGive(motion_mutex_);
            vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));
        }
        ESP_LOGW(TAG,
                 "EnsureTorqueEngagedBeforeMove: re-engagement failed after "
                 "%d attempts; skipping motion entry to avoid silent "
                 "torque-off WritePos.",
                 kMaxReengageRetries);
        return false;
    }

    void MaybeAutoReleaseTorque() {
        if (!auto_release_enabled_.load(std::memory_order_acquire)) {
            return;
        }
        if (!servo_ok_) {
            return;
        }
        if (!boot_init_done_.load(std::memory_order_acquire)) {
            return;
        }
        // PublishTorqueState() raises this when torque re-engages between
        // ServoTask ticks, so a stale idle timer cannot immediately re-OFF.
        if (idle_timer_reset_pending_.exchange(
                false, std::memory_order_acq_rel)) {
            last_motion_end_valid_ = false;
        }
        // Keep the idle window scoped to the currently engaged interval.
        // Released/partial/releasing states must not age a stale timer into
        // the next re-engage.
        if (torque_state_.load(std::memory_order_acquire) !=
            TorqueState::kEngaged) {
            last_motion_end_valid_ = false;
            return;
        }
        if (servo_wobble_active_.load(std::memory_order_acquire)) {
            last_motion_end_valid_ = false;
            return;
        }

        bool moving = motion_driver_->IsMoving();
        uint32_t now_ms =
            static_cast<uint32_t>(esp_timer_get_time() / 1000);

        if (moving) {
            last_motion_end_valid_ = false;
            return;
        }

        if (!last_motion_end_valid_) {
            last_motion_end_ms_ = now_ms;
            last_motion_end_valid_ = true;
            return;
        }

        uint32_t idle_ms = now_ms - last_motion_end_ms_;
        uint32_t timeout_ms =
            auto_release_timeout_ms_.load(std::memory_order_acquire);
        if (idle_ms >= timeout_ms) {
            // Publish the pending OFF before InternalSetServoTorque() can
            // block on motion_mutex_, so a concurrent manual ON is routed
            // through the kReleasing wait/retry path instead of treating the
            // already-expired engaged state as a successful no-op.
            uint32_t my_pre_epoch = MarkReleasing();
            ServoTorqueResult r = InternalSetServoTorque(
                false, false, ReleaseReason::kAutoIdle,
                /*expected_release_epoch=*/my_pre_epoch + 1);
            auto state_after =
                torque_state_.load(std::memory_order_acquire);
            if (state_after == TorqueState::kReleased) {
                last_motion_end_valid_ = false;
            } else if (r.short_circuited) {
                uint32_t current_epoch =
                    torque_release_epoch_.load(std::memory_order_acquire);
                if (current_epoch == my_pre_epoch) {
                    // Auto-idle re-observed motion or wobble under
                    // motion_mutex_ and returned before any bus frame went
                    // out, so the per-axis torque state is still fully
                    // engaged.
                    torque_state_.store(TorqueState::kEngaged,
                                        std::memory_order_release);
                    ESP_LOGW(TAG,
                             "auto-release OFF aborted by motion/wobble "
                             "re-check; rolled back torque_state_ to "
                             "kEngaged (epoch=%u), retry after "
                             "one idle window.",
                             (unsigned)my_pre_epoch);
                } else if (current_epoch == my_pre_epoch + 1) {
                    ESP_LOGW(TAG,
                             "auto-release OFF aborted at bus check; leaving "
                             "torque_state_ for concurrent publisher "
                             "(my_pre_epoch=%u current_epoch=%u).",
                             (unsigned)my_pre_epoch,
                             (unsigned)current_epoch);
                } else {
                    ESP_LOGW(TAG,
                             "auto-release OFF: release epoch advanced past "
                             "ours (my_pre_epoch=%u current_epoch=%u); "
                             "leaving torque_state_ for concurrent publisher.",
                             (unsigned)my_pre_epoch,
                             (unsigned)current_epoch);
                }
                last_motion_end_ms_ = now_ms;
            } else {
                last_motion_end_ms_ = now_ms;
                ESP_LOGW(TAG,
                         "auto-release OFF bus write failed: yaw_ok=%d "
                         "(r=%d) pitch_ok=%d (r=%d). Retrying after one "
                         "idle window.",
                         r.yaw_ok ? 1 : 0, r.yaw_bus_return,
                         r.pitch_ok ? 1 : 0, r.pitch_bus_return);
            }
        }
    }

    // ---- Phase 7: head-touch (Si12T) sensing + reaction ----------------

    // Convenience wrapper around the existing servo write path. Mirrors the
    // math used in the self.robot.set_head_angles MCP tool so that touch
    // reactions and explicit MCP calls produce identical motion.
    //
    // Issue #1: previously this issued WritePos(id, pos, 100, 0) directly,
    // which hung the SCS0009 bus on large-angle reversals (the second
    // servo's frame collided with the first servo still being driven).
    // Now it sets the target and lets the servo_motion task interpolate.
    //
    // The wobble-cancel + StartMove sequence runs under motion_mutex_ so a
    // concurrent ServoWobbleStepAdvance() on servo_motion task cannot pass
    // its active-load gate after this call has cleared
    // servo_wobble_active_ but before it dispatches the user-driven
    // target — which would let a stale wobble step overwrite the new
    // command.
    void WriteHeadAngles(int yaw_deg, int pitch_deg,
                         uint32_t duration_ms = MOTION_DEFAULT_DURATION_MS,
                         bool prefer_linear = false) {
        if (!servo_ok_ || motion_driver_ == nullptr) {
            ESP_LOGW(TAG, "WriteHeadAngles skipped: servo not initialized");
            return;
        }
        if (!TakeMotionMutexAfterTorqueEngaged()) {
            return;
        }
        if (servo_wobble_active_.load()) {
            servo_wobble_active_.store(false);
            servo_wobble_step_.store(0);
        }
        motion_driver_->StartMove(yaw_deg, pitch_deg, duration_ms,
                                  prefer_linear);
        xSemaphoreGive(motion_mutex_);
    }

    // Servo wobble: yaw -A -> +A -> -A -> 0. Each step is dispatched only
    // after the active MotionDriver reports idle, so the delegated path never
    // overwrites an in-flight SCS0009 internal motion.
    //
    // The initial active check stays outside motion_mutex_ so idle ServoTask
    // ticks do not run the torque re-engagement path. The dispatch body runs
    // under motion_mutex_; without that hold, a concurrent non-wobble
    // WriteHeadAngles() could clear servo_wobble_active_ AFTER this function
    // has passed the active-load + IsMoving gate but BEFORE the switch reaches
    // dispatch, which would let a wobble step overwrite the user's freshly-
    // staged target. Holding the mutex makes "wobble-active check → idle check
    // → step dispatch" atomic w.r.t. any external StartMove() request.
    void ServoWobbleStepAdvance() {
        if (!servo_ok_ || motion_driver_ == nullptr) {
            return;
        }
        if (!servo_wobble_active_.load(std::memory_order_acquire)) {
            return;
        }
        if (!TakeMotionMutexAfterTorqueEngaged()) {
            return;
        }
        if (!servo_wobble_active_.load()) {
            xSemaphoreGive(motion_mutex_);
            return;
        }
        // Direct AxisMotion field access under motion_mutex_; calling
        // motion_driver_->IsMoving() here would re-take the non-recursive
        // semaphore and deadlock.
        bool moving = yaw_motion_.moving || pitch_motion_.moving;
        if (moving) {
            xSemaphoreGive(motion_mutex_);
            return;
        }
        const int A = SERVO_WOBBLE_AMPLITUDE_DEG;
        int step = servo_wobble_step_.load();
        int target_yaw = 0;
        // Preserve the current pitch through the wobble sequence; only the
        // yaw axis is animated. A hardcoded `target_pitch = 0` on every
        // step would command the SCS0009 pitch axis toward the lower
        // end-stop (raw pos ~620 ≈ pitch 0°) on a device whose standard
        // rest pose is BOOT_INIT_PITCH_DEG=45°, accelerating #165
        // cumulative WritePos protection-mode onset within a single
        // STROKE gesture. Direct field read is safe here because
        // motion_mutex_ is already held (see contract above). #175.
        int target_pitch = pitch_motion_.current_deg;
        switch (step) {
            case 0: target_yaw = -A; break;
            case 1: target_yaw = +A; break;
            case 2: target_yaw = -A; break;
            case 3: target_yaw =  0; break;
            default:
                servo_wobble_active_.store(false);
                xSemaphoreGive(motion_mutex_);
                return;
        }
        // StartMove contract: caller holds motion_mutex_. Direct call
        // (not via WriteHeadAngles) avoids the re-entrant mutex take.
        motion_driver_->StartMove(target_yaw, target_pitch, SERVO_WOBBLE_STEP_MS);
        servo_wobble_step_.store(step + 1);
        if (step + 1 > 3) {
            servo_wobble_active_.store(false);
        }
        xSemaphoreGive(motion_mutex_);
    }

    void StartServoWobble() {
        if (!servo_ok_ || motion_driver_ == nullptr) {
            ESP_LOGW(TAG, "Servo wobble skipped: servo not initialized");
            return;
        }
        // Stage the wobble; the actual ServoWobbleStepAdvance() is
        // performed on servo_motion task next tick (see ServoTaskMain).
        // Calling ServoWobbleStepAdvance() directly from here — which is
        // commonly reached via the ESP_TIMER_TASK touch-poll callback —
        // would race with the servo_motion task's own per-tick
        // ServoWobbleStepAdvance() at idle: the atomic step load/store
        // does not exclude the read-switch-store compound operation, so
        // two callers can both observe step==0 and end up dispatching
        // step 0 and step 1 concurrently. Centralising advance on the
        // servo_motion task makes the step sequence deterministic, at
        // the cost of a single MOTION_POLL_INTERVAL_MS / MOTION_TICK_MS
        // delay on the first wobble step (well under human perception).
        //
        // motion_mutex_ also serialises this restart against a
        // concurrent ServoWobbleStepAdvance() running on servo_motion:
        // without the hold, a final-step advancement finishing at the
        // same moment as this restart could overwrite our step=0 /
        // active=true with step+1 or active=false, silently dropping
        // the new stroke's wobble. The mutex is staging-only (no UART
        // I/O is performed under it), so taking it from ESP_TIMER_TASK
        // is bounded to sub-millisecond hold time.
        xSemaphoreTake(motion_mutex_, portMAX_DELAY);
        servo_wobble_step_.store(0);
        servo_wobble_active_.store(true);
        xSemaphoreGive(motion_mutex_);
    }

    static void ServoTaskTrampoline(void* arg) {
        static_cast<StackChanBoard*>(arg)->ServoTaskMain();
    }

    void ServoTaskMain() {
        while (true) {
            if (!servo_ok_ || motion_driver_ == nullptr) {
                vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));
                continue;
            }
            motion_driver_->Tick();
            MaybeAutoReleaseTorque();
            ServoWobbleStepAdvance();
            taskYIELD();
        }
    }

    // Schedule a single-shot revert to "idle" face REACTION_HOLD_MS later.
    // Re-arming overwrites any pending revert. Skipped while the avatar
    // has been hidden via set_avatar("off"), so a stale revert timer
    // does not re-cover the LCD after the user explicitly hid it.
    static void TouchRevertCb(void* arg) {
        StackChanBoard* self = static_cast<StackChanBoard*>(arg);
        self->SetAvatarExpressionIfActive("idle");
    }

    void ScheduleIdleRevert() {
        if (touch_revert_timer_ == nullptr) {
            esp_timer_create_args_t args = {
                .callback = &StackChanBoard::TouchRevertCb,
                .arg = this,
                .dispatch_method = ESP_TIMER_TASK,
                .name = "touch_revert",
                .skip_unhandled_events = true,
            };
            ESP_ERROR_CHECK(esp_timer_create(&args, &touch_revert_timer_));
        }
        esp_timer_stop(touch_revert_timer_);  // ok if not running
        esp_timer_start_once(touch_revert_timer_,
                             (uint64_t)REACTION_HOLD_MS * 1000);
    }

    void HandleTap() {
        ESP_LOGI(TAG, "touch event: TAP (zones=%d%d%d raw=0x%02X)",
                 last_zone_snapshot_[0], last_zone_snapshot_[1], last_zone_snapshot_[2],
                 last_output1_raw_);
        last_event_ = TouchEvent::TAP;
        last_event_us_ = esp_timer_get_time();
        // Use the IfActive variant so a tap during set_avatar("off") does
        // not pop the avatar back over the WiFi config / settings screens.
        SetAvatarExpressionIfActive("surprised");
        ScheduleIdleRevert();
    }

    void HandleStroke(uint64_t duration_ms) {
        ESP_LOGI(TAG, "touch event: STROKE (zones=%d%d%d duration=%llums raw=0x%02X)",
                 last_zone_snapshot_[0], last_zone_snapshot_[1], last_zone_snapshot_[2],
                 (unsigned long long)duration_ms, last_output1_raw_);
        last_event_ = TouchEvent::STROKE;
        last_event_us_ = esp_timer_get_time();
        SetAvatarExpressionIfActive("embarrassed");
        StartServoWobble();
        ScheduleIdleRevert();
    }

    // 200 ms periodic poll. Reads the sensor, applies a 2-sample debounce on
    // the OR of the three head zones, and emits TAP/STROKE on falling edges.
    static void TouchPollCb(void* arg) {
        StackChanBoard* self = static_cast<StackChanBoard*>(arg);
        self->TouchPollTick();
    }

    void TouchPollTick() {
        if (!si12t_ok_ || si12t_ == nullptr) {
            return;
        }
        Si12T::TouchState s = si12t_->ReadTouchState();
        if (!s.ok) {
            return;
        }
        // Snapshot for MCP visibility.
        last_output1_raw_ = s.output1_raw;
        last_zone_snapshot_[0] = s.zone[0];
        last_zone_snapshot_[1] = s.zone[1];
        last_zone_snapshot_[2] = s.zone[2];

        bool any_pressed = s.zone[0] || s.zone[1] || s.zone[2];

        // Asymmetric debounce:
        //   press   confirm = 2 samples ( 200 ms) — fast tap detection
        //   release confirm = 4 samples ( 400 ms) — bridges Si12T recalibration
        //                                            and finger-glide gaps that
        //                                            otherwise cut a stroke
        //                                            short and mis-classify it
        //                                            as a tap.
        // Keeping a press "sticky" through brief no-press blips is essential
        // for the stroke gesture to reach STROKE_MIN_MS.
        if (any_pressed == touch_pressed_pending_) {
            touch_pending_count_++;
        } else {
            touch_pending_count_ = 1;
            touch_pressed_pending_ = any_pressed;
        }
        const int needed = touch_pressed_pending_ ? 2 : 4;
        if (touch_pending_count_ < needed) {
            return;  // not yet debounced
        }

        bool now = touch_pressed_pending_;
        if (now == touch_pressed_prev_) {
            return;  // no edge
        }

        uint64_t now_us = esp_timer_get_time();

        if (now) {
            // Rising edge.
            if (now_us < cooldown_until_us_) {
                // Suppress press event while in post-reaction cooldown.
                touch_pressed_prev_ = now;
                touch_press_start_us_ = now_us;
                return;
            }
            touch_pressed_prev_ = true;
            touch_press_start_us_ = now_us;
        } else {
            // Falling edge: classify by hold duration.
            touch_pressed_prev_ = false;
            uint64_t duration_ms = (now_us - touch_press_start_us_) / 1000ULL;
            if (now_us < cooldown_until_us_) {
                // We were in cooldown when pressed — drop the release event too.
                return;
            }
            if (duration_ms >= STROKE_MIN_MS) {
                HandleStroke(duration_ms);
            } else {
                // Treat the 400-600 ms grey zone as TAP.
                HandleTap();
            }
            cooldown_until_us_ = now_us + (uint64_t)COOLDOWN_MS * 1000ULL;
        }
    }

    void InitializeSi12tTouch() {
        ESP_LOGI(TAG, "Init Si12T head-touch sensor (I2C addr 0x%02X)", Si12T::DEFAULT_ADDR);
        si12t_ = std::unique_ptr<Si12T>(new Si12T(i2c_bus_));
        si12t_ok_ = si12t_->Begin();
        if (!si12t_ok_) {
            ESP_LOGW(TAG, "Si12T not detected; head-touch disabled (other features unaffected)");
            si12t_.reset();
            return;
        }

        esp_timer_create_args_t poll_args = {
            .callback = &StackChanBoard::TouchPollCb,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "touch_poll",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&poll_args, &touch_poll_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(touch_poll_timer_,
                                                 (uint64_t)TOUCH_POLL_MS * 1000));
        ESP_LOGI(TAG, "Si12T touch poll started (%d ms interval)", TOUCH_POLL_MS);
    }

    // Map a face name (idle/happy/...) to the embedded RGB565 image.
    // Returns nullptr if the name is unknown.
    static const lv_image_dsc_t* AvatarImageFor(const char* face) {
        if (face == nullptr) return nullptr;
        if (strcmp(face, "idle") == 0)        return &avatar_idle;
        if (strcmp(face, "happy") == 0)       return &avatar_happy;
        if (strcmp(face, "thinking") == 0)    return &avatar_thinking;
        if (strcmp(face, "sad") == 0)         return &avatar_sad;
        if (strcmp(face, "surprised") == 0)   return &avatar_surprised;
        if (strcmp(face, "embarrassed") == 0) return &avatar_embarrassed;
        return nullptr;
    }

    // Create avatar_img_ on the active LVGL screen, scaled to fill the LCD.
    // Caller must hold the LVGL/display lock. Returns true on success or
    // when avatar_img_ already exists.
    bool EnsureAvatarObject() {
        if (avatar_img_ != nullptr) {
            return true;
        }
        lv_obj_t* screen = lv_screen_active();
        if (screen == nullptr) {
            return false;
        }
        avatar_img_ = lv_image_create(screen);
        if (avatar_img_ == nullptr) {
            return false;
        }
        // Center on the 320x240 LCD and upscale 160x120 -> ~320x240 (2x).
        // lv_image_set_scale uses 256 = 1.0x; 512 = 2.0x.
        lv_image_set_scale(avatar_img_, 512);
        lv_obj_align(avatar_img_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_clear_flag(avatar_img_, LV_OBJ_FLAG_SCROLLABLE);
        // Keep the avatar visually on top of the chat UI's emoji_label_,
        // chat bubbles, etc. The status bar (clock/battery) lives on a
        // separate sibling and is moved to foreground later if needed.
        lv_obj_move_foreground(avatar_img_);
        ESP_LOGI(TAG, "Avatar lv_image created on active screen");
        return true;
    }

    // Apply the requested face to avatar_img_. Returns false if the face is
    // unknown or the avatar object cannot be created yet.
    bool SetAvatarExpressionLocked(const char* face) {
        const lv_image_dsc_t* dsc = AvatarImageFor(face);
        if (dsc == nullptr) {
            return false;
        }
        if (!EnsureAvatarObject()) {
            return false;
        }
        lv_image_set_src(avatar_img_, dsc);
        lv_obj_move_foreground(avatar_img_);
        current_avatar_face_ = face;
        return true;
    }

    // Public-style entry that takes the display lock. Used by the MCP tool
    // and by the deferred init timer. Always safe to call from any task.
    //
    // Also handles the "resume from off" path: if the previous face was
    // "off" (avatar layer hidden), the layer is unhidden here, and if blink
    // was enabled before SetAvatarOff() ran it is restored automatically.
    bool SetAvatarExpression(const char* face) {
        if (display_ == nullptr) {
            ESP_LOGW(TAG, "SetAvatarExpression('%s') ignored: display_ not ready", face);
            return false;
        }
        bool was_off = (current_avatar_face_ == "off");
        bool ok;
        {
            DisplayLockGuard lock(display_);
            if (avatar_img_ != nullptr) {
                // Restore visibility if a previous SetAvatarOff() hid the
                // layer. Cheap no-op when the flag is already clear.
                lv_obj_clear_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
            }
            ok = SetAvatarExpressionLocked(face);
        }
        if (!ok) {
            ESP_LOGW(TAG, "SetAvatarExpression('%s') deferred (face unknown or screen not ready)", face);
            return ok;
        }
        // Coming back from "off": if blink was on before going off, restart
        // it so the user does not have to re-issue set_blink. The default
        // experience is "blink follows the avatar".
        if (was_off && blink_enabled_before_off_) {
            StartBlinkTimer();
            ESP_LOGI(TAG, "Blink restored after avatar resume from OFF");
        }
        blink_enabled_before_off_ = false;
        return ok;
    }

    // Hide the avatar layer and disable blink so the underlying
    // xiaozhi-esp32 screens (WiFi config UI, OTA, settings) become visible.
    // The avatar lv_obj is kept allocated so a subsequent
    // SetAvatarExpression(<other face>) can re-show it cheaply, and the
    // previous blink state is remembered for restoration.
    bool SetAvatarOff() {
        if (display_ == nullptr) {
            ESP_LOGW(TAG, "SetAvatarOff() ignored: display_ not ready");
            return false;
        }
        // Capture the previous blink state only on the first transition
        // into "off". A repeated set_avatar("off") while already off must
        // not overwrite the saved state with the post-off (always-false)
        // blink_enabled_.
        if (current_avatar_face_ != "off") {
            blink_enabled_before_off_ = blink_enabled_;
        }
        // StopBlinkTimer() takes the display lock internally for the
        // resting-face restore step, so call it before grabbing our lock.
        StopBlinkTimer();
        {
            DisplayLockGuard lock(display_);
            if (avatar_img_ != nullptr) {
                lv_obj_add_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
            }
        }
        current_avatar_face_ = "off";
        ESP_LOGI(TAG, "Avatar OFF: hidden + blink disabled (was %s)",
                 blink_enabled_before_off_ ? "ON" : "OFF");
        return true;
    }

    // Internal-only entry used by autonomous animations (touch reactions,
    // idle revert, etc.). Skips the face change while the avatar is in
    // the user-requested "off" state, so the underlying xiaozhi-esp32
    // screens remain visible. Returns true if the face was applied.
    bool SetAvatarExpressionIfActive(const char* face) {
        if (current_avatar_face_ == "off") {
            ESP_LOGD(TAG, "SetAvatarExpressionIfActive('%s') skipped: avatar is OFF", face);
            return false;
        }
        return SetAvatarExpression(face);
    }

    // Schedule a one-shot/periodic timer that keeps trying to install the
    // initial avatar image until the LVGL screen tree is ready (i.e. after
    // Application::Start() has run Display::SetupUI()).
    void InitializeAvatar() {
        ESP_LOGI(TAG, "Schedule avatar init (deferred until SetupUI completes)");
        esp_timer_create_args_t timer_args = {
            .callback = [](void* arg) {
                StackChanBoard* board = static_cast<StackChanBoard*>(arg);
                if (board->SetAvatarExpression("idle")) {
                    ESP_LOGI(TAG, "Initial avatar (idle) installed");
                    if (board->avatar_init_timer_ != nullptr) {
                        esp_timer_stop(board->avatar_init_timer_);
                    }
                }
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "avatar_init",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &avatar_init_timer_));
        // Retry every 500 ms; SetupUI() typically completes within a few
        // hundred ms after Application::Start(). Once installed the callback
        // stops the timer.
        ESP_ERROR_CHECK(esp_timer_start_periodic(avatar_init_timer_, 500 * 1000));
    }

    // ---- Phase 2: parts (eyes / mouth) and blink state machine ----------
    //
    // Eye state images (avatar_eyes_open / _half / _closed) are referenced
    // directly by the blink state machine; we don't expose a generic
    // EyesImageFor() helper yet because no MCP tool sets eyes manually.

    // Map a mouth shape name to its full-frame image. Returns nullptr if unknown.
    static const lv_image_dsc_t* MouthImageFor(const char* shape) {
        if (shape == nullptr) return nullptr;
        if (strcmp(shape, "closed") == 0) return &avatar_mouth_closed;
        if (strcmp(shape, "half") == 0)   return &avatar_mouth_half;
        if (strcmp(shape, "open") == 0)   return &avatar_mouth_open;
        if (strcmp(shape, "e") == 0)      return &avatar_mouth_e;
        if (strcmp(shape, "u") == 0)      return &avatar_mouth_u;
        return nullptr;
    }

    // Swap avatar_img_ to a part image (eye or mouth). Caller must hold lock.
    // Does NOT touch current_avatar_face_, so SetAvatarExpression(...) called
    // later will still know what face to "return to".
    bool SetPartImageLocked(const lv_image_dsc_t* dsc) {
        if (dsc == nullptr) return false;
        if (!EnsureAvatarObject()) return false;
        lv_image_set_src(avatar_img_, dsc);
        lv_obj_move_foreground(avatar_img_);
        return true;
    }

    // Restore the last full-face expression after a part overlay.
    bool RestoreCurrentFaceLocked() {
        const lv_image_dsc_t* dsc = AvatarImageFor(current_avatar_face_.c_str());
        if (dsc == nullptr) {
            // Fall back to idle if somehow stale.
            dsc = &avatar_idle;
        }
        if (!EnsureAvatarObject()) return false;
        lv_image_set_src(avatar_img_, dsc);
        lv_obj_move_foreground(avatar_img_);
        return true;
    }

    // Public mouth setter: wraps lock + look-up.
    bool SetMouthShape(const char* shape) {
        if (display_ == nullptr) {
            ESP_LOGW(TAG, "SetMouthShape('%s') ignored: display_ not ready", shape);
            return false;
        }
        const lv_image_dsc_t* dsc = MouthImageFor(shape);
        if (dsc == nullptr) {
            return false;
        }
        DisplayLockGuard lock(display_);
        return SetPartImageLocked(dsc);
    }

    // Step callback for the four-phase blink sequence. Each invocation
    // advances blink_state_, applies the corresponding image, and re-arms
    // blink_step_timer_ unless we're returning to the resting face.
    static void BlinkStepCb(void* arg) {
        StackChanBoard* self = static_cast<StackChanBoard*>(arg);
        self->BlinkStepAdvance();
    }

    void BlinkStepAdvance() {
        if (display_ == nullptr) {
            blink_state_ = BlinkState::IDLE;
            return;
        }
        DisplayLockGuard lock(display_);
        switch (blink_state_) {
            case BlinkState::EYES_HALF_DOWN:
                SetPartImageLocked(&avatar_eyes_closed);
                blink_state_ = BlinkState::EYES_CLOSED;
                esp_timer_start_once(blink_step_timer_, BLINK_STEP_MS * 1000);
                break;
            case BlinkState::EYES_CLOSED:
                SetPartImageLocked(&avatar_eyes_half);
                blink_state_ = BlinkState::EYES_HALF_UP;
                esp_timer_start_once(blink_step_timer_, BLINK_STEP_MS * 1000);
                break;
            case BlinkState::EYES_HALF_UP:
                // Final: restore the last applied face (Phase 2 trade-off:
                // any active mouth overlay is replaced by the face image).
                RestoreCurrentFaceLocked();
                blink_state_ = BlinkState::IDLE;
                break;
            case BlinkState::IDLE:
            default:
                // Stale callback; nothing to do.
                break;
        }
    }

    // Schedule callback: fires roughly every BLINK_MIN_GAP_MS..BLINK_MAX_GAP_MS.
    // Starts a new blink if enabled and not already blinking, then re-arms
    // itself with a fresh random interval.
    static void BlinkScheduleCb(void* arg) {
        StackChanBoard* self = static_cast<StackChanBoard*>(arg);
        self->BlinkScheduleTick();
    }

    void BlinkScheduleTick() {
        if (blink_enabled_ && blink_state_ == BlinkState::IDLE && display_ != nullptr) {
            // Begin the blink: half-down now, full-closed at next step.
            DisplayLockGuard lock(display_);
            if (SetPartImageLocked(&avatar_eyes_half)) {
                blink_state_ = BlinkState::EYES_HALF_DOWN;
                esp_timer_start_once(blink_step_timer_, BLINK_STEP_MS * 1000);
            }
        }
        // Re-arm scheduler with a fresh random interval, even if we skipped
        // this blink (e.g. avatar not yet on screen). This keeps the cadence
        // organic instead of clumping after a long pause.
        if (blink_enabled_) {
            uint32_t span_ms = BLINK_MAX_GAP_MS - BLINK_MIN_GAP_MS;
            uint32_t next_ms = BLINK_MIN_GAP_MS + (esp_random() % span_ms);
            esp_timer_start_once(blink_schedule_timer_, (uint64_t)next_ms * 1000);
        }
    }

    void EnsureBlinkTimers() {
        if (blink_step_timer_ == nullptr) {
            esp_timer_create_args_t step_args = {
                .callback = &StackChanBoard::BlinkStepCb,
                .arg = this,
                .dispatch_method = ESP_TIMER_TASK,
                .name = "blink_step",
                .skip_unhandled_events = true,
            };
            ESP_ERROR_CHECK(esp_timer_create(&step_args, &blink_step_timer_));
        }
        if (blink_schedule_timer_ == nullptr) {
            esp_timer_create_args_t sched_args = {
                .callback = &StackChanBoard::BlinkScheduleCb,
                .arg = this,
                .dispatch_method = ESP_TIMER_TASK,
                .name = "blink_sched",
                .skip_unhandled_events = true,
            };
            ESP_ERROR_CHECK(esp_timer_create(&sched_args, &blink_schedule_timer_));
        }
    }

    void StartBlinkTimer() {
        EnsureBlinkTimers();
        blink_enabled_ = true;
        // Make sure no leftover schedule timer is running, then arm one with
        // a fresh random interval.
        esp_timer_stop(blink_schedule_timer_);
        uint32_t span_ms = BLINK_MAX_GAP_MS - BLINK_MIN_GAP_MS;
        uint32_t first_ms = BLINK_MIN_GAP_MS + (esp_random() % span_ms);
        esp_timer_start_once(blink_schedule_timer_, (uint64_t)first_ms * 1000);
        ESP_LOGI(TAG, "Blink ENABLED (first blink in %u ms)", (unsigned)first_ms);
    }

    void StopBlinkTimer() {
        blink_enabled_ = false;
        if (blink_schedule_timer_ != nullptr) {
            esp_timer_stop(blink_schedule_timer_);
        }
        if (blink_step_timer_ != nullptr) {
            esp_timer_stop(blink_step_timer_);
        }
        // If we stopped mid-sequence, snap back to the resting face so the
        // user is not left staring at half-closed eyes.
        if (blink_state_ != BlinkState::IDLE && display_ != nullptr) {
            DisplayLockGuard lock(display_);
            RestoreCurrentFaceLocked();
        }
        blink_state_ = BlinkState::IDLE;
        ESP_LOGI(TAG, "Blink DISABLED");
    }

    // ---- Phase 4 audio (Issue #76): TTS state-driven lip-sync ----------
    //
    // While the gateway is playing TTS audio (tts.start..tts.stop), cycle
    // the mouth shape through CLOSED -> HALF -> OPEN -> HALF on a fixed
    // TTS_LIPSYNC_STEP_MS cadence. This is the (A) state-driven approach
    // proposed in Issue #76; the (B) audio-envelope-driven follow-up will
    // replace this cycle with a per-frame amplitude mapping in a separate
    // change.
    //
    // Concurrency:
    //   - Single esp_timer self-rearming on ESP_TIMER_TASK.
    //   - Coexists with the mouth-sequence playback task: when
    //     mouth_seq_active_ is true (the user issued a set_mouth_sequence
    //     while we were animating), the lip-sync step yields its frame and
    //     re-arms; the user-issued sequence wins until it completes, then
    //     lip-sync resumes naturally on the next tick.
    //   - Pauses autonomous blink while active (same Phase 2 reasoning as
    //     the mouth-sequence task: BlinkStepAdvance()'s
    //     RestoreCurrentFaceLocked() would overwrite the mouth overlay).
    //     Restores blink at stop based on blink_desired_ so a set_blink
    //     issued mid-playback is honoured.
    static void TtsLipSyncStepCb(void* arg) {
        static_cast<StackChanBoard*>(arg)->TtsLipSyncStepAdvance();
    }

    void TtsLipSyncStepAdvance() {
        if (!tts_lipsync_active_.load(std::memory_order_acquire)) {
            return;
        }
        if (display_ == nullptr) {
            return;
        }
        // Yield to an in-flight user-issued mouth sequence; re-arm so we
        // resume on our cadence as soon as the sequence finishes.
        if (mouth_seq_active_.load(std::memory_order_acquire)) {
            esp_timer_start_once(tts_lipsync_timer_,
                                 (uint64_t)TTS_LIPSYNC_STEP_MS * 1000);
            return;
        }
        const char* shape = nullptr;
        switch (tts_lipsync_shape_) {
            case TtsLipSyncShape::CLOSED:
                shape = "half";
                tts_lipsync_shape_ = TtsLipSyncShape::HALF_RISING;
                break;
            case TtsLipSyncShape::HALF_RISING:
                shape = "open";
                tts_lipsync_shape_ = TtsLipSyncShape::OPEN;
                break;
            case TtsLipSyncShape::OPEN:
                shape = "half";
                tts_lipsync_shape_ = TtsLipSyncShape::HALF_FALLING;
                break;
            case TtsLipSyncShape::HALF_FALLING:
            default:
                shape = "closed";
                tts_lipsync_shape_ = TtsLipSyncShape::CLOSED;
                break;
        }
        SetMouthShape(shape);
        if (tts_lipsync_active_.load(std::memory_order_acquire)) {
            esp_timer_start_once(tts_lipsync_timer_,
                                 (uint64_t)TTS_LIPSYNC_STEP_MS * 1000);
        }
    }

    void EnsureTtsLipSyncTimer() {
        if (tts_lipsync_timer_ == nullptr) {
            esp_timer_create_args_t args = {
                .callback = &StackChanBoard::TtsLipSyncStepCb,
                .arg = this,
                .dispatch_method = ESP_TIMER_TASK,
                .name = "tts_lipsync",
                .skip_unhandled_events = true,
            };
            ESP_ERROR_CHECK(esp_timer_create(&args, &tts_lipsync_timer_));
        }
    }

    void StartTtsLipSync() {
        if (display_ == nullptr) {
            ESP_LOGD(TAG, "StartTtsLipSync ignored: display_ not ready");
            return;
        }
        EnsureTtsLipSyncTimer();
        if (tts_lipsync_active_.exchange(true, std::memory_order_acq_rel)) {
            // Already active (e.g. duplicate tts.start); nothing to do.
            return;
        }
        // Pause autonomous blink so BlinkStepAdvance()'s
        // RestoreCurrentFaceLocked() does not overwrite the mouth overlay.
        // blink_desired_ remembers the user's intent for restore at stop.
        StopBlinkTimer();
        // Start the cycle from a known resting position so the first audible
        // frame opens the mouth from closed.
        tts_lipsync_shape_ = TtsLipSyncShape::CLOSED;
        SetMouthShape("closed");
        esp_timer_start_once(tts_lipsync_timer_,
                             (uint64_t)TTS_LIPSYNC_STEP_MS * 1000);
        ESP_LOGI(TAG, "TTS lip-sync STARTED (cycle=%d ms)",
                 TTS_LIPSYNC_STEP_MS);
    }

    void StopTtsLipSync() {
        if (!tts_lipsync_active_.exchange(false, std::memory_order_acq_rel)) {
            return;  // already stopped
        }
        if (tts_lipsync_timer_ != nullptr) {
            esp_timer_stop(tts_lipsync_timer_);
        }
        // If a user-issued mouth sequence is in flight (we were yielding
        // our frames to it via the mouth_seq_active_ guard in
        // TtsLipSyncStepAdvance), let the sequence task own both the
        // mouth shape and the blink restore at sequence end. Touching
        // either here would race the sequence:
        //   - SetMouthShape("closed") would clobber the user's current
        //     frame mid-sequence;
        //   - StartBlinkTimer() would let BlinkStepAdvance()'s
        //     RestoreCurrentFaceLocked() overwrite the mouth overlay
        //     before the sequence finishes drawing (Phase 2 trade-off).
        // The sequence task already restores blink from blink_desired_
        // at its own end (see MouthSequenceTaskLoop), so deferring is
        // safe and idempotent.
        if (mouth_seq_active_.load(std::memory_order_acquire)) {
            ESP_LOGI(TAG,
                     "TTS lip-sync STOPPED (mouth_seq active; deferring "
                     "mouth + blink restore to sequence end)");
            return;
        }
        // Snap back to a closed mouth so the device does not freeze on a
        // half-open frame.
        if (display_ != nullptr) {
            SetMouthShape("closed");
        }
        // Restore blink based on the user's most recent intent (mirrors the
        // mouth-sequence playback task's restore semantics).
        if (blink_desired_.load(std::memory_order_acquire)) {
            StartBlinkTimer();
        }
        ESP_LOGI(TAG, "TTS lip-sync STOPPED");
    }

    // ---- Phase 2: lip-sync sequence playback (Issue #5) ----------------
    //
    // set_mouth_sequence accepts a list of {shape, duration_ms} pairs and
    // walks through it on a dedicated FreeRTOS task. Each step swaps the
    // mouth-only image and waits duration_ms before advancing. Walking the
    // queue locally avoids the per-step WebSocket RTT jitter that callers
    // see when issuing many set_mouth calls back-to-back from a TTS loop.
    //
    // Concurrency model:
    //   - mouth_seq_lock_ protects mouth_seq_pending_.
    //   - mouth_seq_signal_ is a binary semaphore that wakes the task when
    //     a new sequence has been enqueued.
    //   - mouth_seq_active_ / mouth_seq_cancel_requested_ are volatile flags
    //     read by the task between steps. Setting cancel_requested while the
    //     task is sleeping in vTaskDelay simply means the task picks up the
    //     cancel at the next slice boundary (kMouthCancelSliceMs apart).
    //   - Re-entry semantics: a fresh set_mouth_sequence call replaces the
    //     pending queue and marks cancel_requested so the task drops the
    //     remainder of the current sequence and starts the new one.
    //   - Interrupt sources: set_mouth, set_avatar, and set_mouth_sequence
    //     all call RequestMouthSequenceCancel() before mutating display
    //     state, so a sequence in flight is cleanly preempted.
    //
    // Trade-offs:
    //   - The MCP Property type system does not support array values, so
    //     the gateway serialises `steps` to a JSON string and passes it as
    //     `steps_json`. Validation happens here once, atomically: if any
    //     step is malformed the whole call is rejected and nothing is
    //     queued (no half-played sequences).
    //   - Autonomous blink is paused while a sequence plays because the
    //     blink state machine ends by calling RestoreCurrentFaceLocked(),
    //     which would replace the active mouth overlay with the resting
    //     face image (see Phase 2 comment near BlinkStepAdvance()).
    //   - The final shape is held after the sequence finishes; callers
    //     that want the mouth to close at the end should append a
    //     {"closed", N} step explicitly. This keeps the primitive composable
    //     with future expression-style use cases (e.g. ending on an open
    //     smile).
    static constexpr int kMaxMouthSequenceSteps = 256;
    static constexpr int kMouthStepMinMs = 10;
    static constexpr int kMouthStepMaxMs = 10000;
    static constexpr uint32_t kMouthCancelSliceMs = 20;

    struct MouthStep {
        std::string shape;
        uint32_t duration_ms;
    };

    struct MouthSequenceEnqueueResult {
        bool ok;
        std::string error;
        int queued_steps;
        uint32_t total_duration_ms;
    };

    TaskHandle_t mouth_seq_task_ = nullptr;
    SemaphoreHandle_t mouth_seq_lock_ = nullptr;     // protects mouth_seq_pending_ + generation
    SemaphoreHandle_t mouth_seq_signal_ = nullptr;   // binary semaphore: wake the task
    std::vector<MouthStep> mouth_seq_pending_;
    std::atomic<bool> mouth_seq_active_{false};
    std::atomic<bool> mouth_seq_cancel_requested_{false};
    // Generation counter bumped under mouth_seq_lock_ by every preemption
    // path (set_mouth, set_avatar, fresh set_mouth_sequence). The playback
    // task latches a snapshot at sequence start and re-checks before every
    // SetMouthShape() call, so a preempt issued in the 0..kMouthCancelSliceMs
    // window between the last cancel-flag check and the next SetMouthShape
    // call still aborts the current frame draw. Without this, the task
    // could draw one stale mouth frame after the user-issued set_mouth /
    // set_avatar handler had already returned.
    std::atomic<uint32_t> mouth_seq_generation_{0};
    // User's explicitly-requested blink state, independent of whether
    // a mouth sequence is currently suppressing the timer. set_blink
    // updates this; the playback task restores StartBlinkTimer() at
    // sequence end iff this is true. Without this split a set_blink
    // call issued during a sequence is silently overwritten by the
    // pre-sequence snapshot when the task finishes.
    std::atomic<bool> blink_desired_{false};

    // Mark any in-flight or pending sequence for cancellation. Safe to
    // call from any thread. Takes mouth_seq_lock_ so that:
    //   - mouth_seq_pending_ is cleared atomically (callers that issue
    //     set_mouth / set_avatar in the brief window between
    //     EnqueueMouthSequence() returning and the task waking up don't
    //     get overwritten by the queued-but-not-yet-active sequence);
    //   - mouth_seq_cancel_requested_ is set so the task aborts at the
    //     next slice boundary if it is already active;
    //   - mouth_seq_generation_ is bumped under release ordering so the
    //     task observes a stale generation at its next per-step check
    //     and skips the remaining SetMouthShape() calls.
    // Idempotent under repeated calls.
    void RequestMouthSequenceCancel() {
        if (mouth_seq_lock_ == nullptr) {
            return;
        }
        if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
            mouth_seq_pending_.clear();
            mouth_seq_cancel_requested_.store(true, std::memory_order_release);
            mouth_seq_generation_.fetch_add(1, std::memory_order_release);
            xSemaphoreGive(mouth_seq_lock_);
        }
    }

    // Parse and validate a JSON-serialised sequence, then atomically
    // replace mouth_seq_pending_ and signal the playback task. Returns
    // a populated MouthSequenceEnqueueResult; on validation failure
    // nothing is queued.
    MouthSequenceEnqueueResult EnqueueMouthSequence(const std::string& steps_json) {
        MouthSequenceEnqueueResult r{false, std::string(), 0, 0};

        cJSON* root = cJSON_Parse(steps_json.c_str());
        if (root == nullptr) {
            r.error = "steps must be a JSON array (parse failed)";
            return r;
        }
        if (!cJSON_IsArray(root)) {
            r.error = "steps must be a JSON array";
            cJSON_Delete(root);
            return r;
        }
        int n = cJSON_GetArraySize(root);
        if (n < 1 || n > kMaxMouthSequenceSteps) {
            r.error = std::string("steps length out of range (1..") +
                      std::to_string(kMaxMouthSequenceSteps) + ")";
            cJSON_Delete(root);
            return r;
        }

        std::vector<MouthStep> parsed;
        parsed.reserve(static_cast<size_t>(n));
        uint32_t total = 0;
        for (int i = 0; i < n; ++i) {
            cJSON* item = cJSON_GetArrayItem(root, i);
            if (!cJSON_IsObject(item)) {
                r.error = std::string("step[") + std::to_string(i) + "] must be an object";
                cJSON_Delete(root);
                return r;
            }
            cJSON* shape = cJSON_GetObjectItem(item, "shape");
            cJSON* dur = cJSON_GetObjectItem(item, "duration_ms");
            if (!cJSON_IsString(shape) || shape->valuestring == nullptr) {
                r.error = std::string("step[") + std::to_string(i) + "].shape must be a string";
                cJSON_Delete(root);
                return r;
            }
            if (!cJSON_IsNumber(dur)) {
                r.error = std::string("step[") + std::to_string(i) + "].duration_ms must be an integer";
                cJSON_Delete(root);
                return r;
            }
            if (MouthImageFor(shape->valuestring) == nullptr) {
                r.error = std::string("step[") + std::to_string(i) +
                          "].shape unknown: '" + shape->valuestring +
                          "' (allowed: closed, half, open, e, u)";
                cJSON_Delete(root);
                return r;
            }
            int d = dur->valueint;
            if (d < kMouthStepMinMs || d > kMouthStepMaxMs) {
                r.error = std::string("step[") + std::to_string(i) +
                          "].duration_ms out of range (" +
                          std::to_string(kMouthStepMinMs) + ".." +
                          std::to_string(kMouthStepMaxMs) + ")";
                cJSON_Delete(root);
                return r;
            }
            parsed.push_back({std::string(shape->valuestring),
                              static_cast<uint32_t>(d)});
            total += static_cast<uint32_t>(d);
        }
        cJSON_Delete(root);

        if (mouth_seq_lock_ == nullptr || mouth_seq_signal_ == nullptr ||
            mouth_seq_task_ == nullptr) {
            r.error = "mouth sequence task not initialised";
            return r;
        }

        // Atomically replace the pending queue and mark any in-flight
        // sequence for cancellation so it stops at the next slice. The
        // generation bump is what makes a fresh enqueue preempt the
        // currently-playing sequence even between cancel-flag checks
        // and SetMouthShape() calls (per-step generation re-check in
        // MouthSequenceTaskLoop).
        if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
            mouth_seq_pending_ = std::move(parsed);
            if (mouth_seq_active_.load(std::memory_order_acquire)) {
                mouth_seq_cancel_requested_.store(true, std::memory_order_release);
            }
            mouth_seq_generation_.fetch_add(1, std::memory_order_release);
            xSemaphoreGive(mouth_seq_lock_);
        }
        // Wake the task. If the task is already running through a previous
        // sequence, it will pick up the new pending queue after observing
        // cancel_requested at the next slice and looping back.
        xSemaphoreGive(mouth_seq_signal_);

        r.ok = true;
        r.queued_steps = n;
        r.total_duration_ms = total;
        return r;
    }

    static void MouthSequenceTaskTrampoline(void* arg) {
        static_cast<StackChanBoard*>(arg)->MouthSequenceTaskLoop();
    }

    void MouthSequenceTaskLoop() {
        for (;;) {
            // Wait until something is enqueued (or self-signaled at the
            // tail of a previous run when more pending was discovered).
            xSemaphoreTake(mouth_seq_signal_, portMAX_DELAY);

            // Drain whatever is pending right now into a local copy so
            // we can release the lock before walking the sequence. Latch
            // the generation under the same lock so we can reject any
            // newer preempt at the next per-step check.
            std::vector<MouthStep> seq;
            uint32_t my_generation = 0;
            if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
                seq = std::move(mouth_seq_pending_);
                mouth_seq_pending_.clear();
                mouth_seq_cancel_requested_.store(false, std::memory_order_release);
                mouth_seq_active_.store(!seq.empty(), std::memory_order_release);
                my_generation = mouth_seq_generation_.load(std::memory_order_acquire);
                xSemaphoreGive(mouth_seq_lock_);
            }
            if (seq.empty()) {
                continue;
            }

            // Pause autonomous blink for the duration of the sequence so
            // BlinkStepAdvance()'s RestoreCurrentFaceLocked() does not
            // overwrite the active mouth overlay. Note: we no longer
            // snapshot blink_enabled_ here — the user's intent is read
            // from blink_desired_ at sequence end so calls to set_blink
            // made during playback are honoured.
            StopBlinkTimer();

            for (const auto& step : seq) {
                // Re-check cancel + generation right before each frame
                // draw. Any preempt issued between this check and the
                // previous SetMouthShape() will be observed here, so we
                // never draw a frame after a newer set_mouth / set_avatar
                // / set_mouth_sequence handler has returned to the caller.
                if (mouth_seq_cancel_requested_.load(std::memory_order_acquire) ||
                    mouth_seq_generation_.load(std::memory_order_acquire) != my_generation) {
                    break;
                }
                SetMouthShape(step.shape.c_str());
                // Sleep in small slices so cancel is observed quickly.
                uint32_t remaining = step.duration_ms;
                while (remaining > 0 &&
                       !mouth_seq_cancel_requested_.load(std::memory_order_acquire) &&
                       mouth_seq_generation_.load(std::memory_order_acquire) == my_generation) {
                    uint32_t slice = remaining > kMouthCancelSliceMs
                                         ? kMouthCancelSliceMs
                                         : remaining;
                    vTaskDelay(pdMS_TO_TICKS(slice));
                    remaining -= slice;
                }
            }

            // Restore blink according to the user's most recent intent,
            // not a snapshot taken before the sequence started. This way
            // a set_blink(true/false) issued during the sequence is the
            // one that wins at the end.
            if (blink_desired_.load(std::memory_order_acquire)) {
                StartBlinkTimer();
            }

            // If a fresh sequence was enqueued during playback, the
            // cancel path above will have left it in mouth_seq_pending_.
            // Self-signal so the next outer-loop iteration picks it up
            // immediately rather than parking on the semaphore.
            bool has_more = false;
            if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
                mouth_seq_active_.store(false, std::memory_order_release);
                has_more = !mouth_seq_pending_.empty();
                xSemaphoreGive(mouth_seq_lock_);
            }
            if (has_more) {
                xSemaphoreGive(mouth_seq_signal_);
            }
        }
    }

    void InitializeMouthSequenceTask() {
        if (mouth_seq_task_ != nullptr) {
            return;
        }
        mouth_seq_lock_ = xSemaphoreCreateMutex();
        mouth_seq_signal_ = xSemaphoreCreateBinary();
        if (mouth_seq_lock_ == nullptr || mouth_seq_signal_ == nullptr) {
            ESP_LOGE(TAG, "Failed to create mouth sequence sync primitives");
            if (mouth_seq_lock_ != nullptr) {
                vSemaphoreDelete(mouth_seq_lock_);
                mouth_seq_lock_ = nullptr;
            }
            if (mouth_seq_signal_ != nullptr) {
                vSemaphoreDelete(mouth_seq_signal_);
                mouth_seq_signal_ = nullptr;
            }
            return;
        }
        BaseType_t ok = xTaskCreate(&StackChanBoard::MouthSequenceTaskTrampoline,
                                    "mouth_seq", 4096, this,
                                    tskIDLE_PRIORITY + 2,
                                    &mouth_seq_task_);
        if (ok != pdPASS) {
            ESP_LOGE(TAG, "Failed to create mouth_seq task");
            vSemaphoreDelete(mouth_seq_lock_);
            mouth_seq_lock_ = nullptr;
            vSemaphoreDelete(mouth_seq_signal_);
            mouth_seq_signal_ = nullptr;
            mouth_seq_task_ = nullptr;
        } else {
            ESP_LOGI(TAG, "Mouth sequence task ready");
        }
    }

    void RegisterMcpTools() {
        auto& mcp_server = McpServer::GetInstance();
        ESP_LOGI(TAG, "Registering StackChan MCP tools...");

        // Set head angles (yaw, pitch in degrees)
        // SCS0009: 1 step = 0.3125 degrees, so 1 degree = 3.2 steps (= 16/5)
        // yaw: -90..90 degrees (no hardware restriction). pitch: two-tier
        // guard — see SAFE_PITCH_MIN/MAX (hard clamp for mechanical safety)
        // and RECOMMENDED_PITCH_MIN/MAX (M5Stack-documented operating sweet
        // spot) above, plus Issue #80 / #98.
        mcp_server.AddTool(
            "self.robot.set_head_angles",
            "Set the head angles of the robot. yaw: horizontal (-90 to 90). pitch: vertical. M5Stack-recommended operating range is 5 to 85 degrees per https://docs.m5stack.com/en/StackChan (\"Motion Angle Notice\"). The firmware also accepts values up to 88 degrees (the hard clamp guards against the audible sub-stall observed at pitch=89 on real hardware), but values outside 5-85 degrees are not officially endorsed and may stress the servo over time. Requests below 0 degrees or above 88 degrees are silently clamped with an ESP_LOGW. See README \"Hardware safety notes\".",
            // Pitch schema range is intentionally permissive across the
            // entire `int` value range (std::numeric_limits<int>::min/max):
            // the authoritative Tier 1 enforcement lives in the handler
            // below (silent clamp to [SAFE_PITCH_MIN, SAFE_PITCH_MAX] with
            // ESP_LOGW). Any narrower range would cause McpServer::Property
            // to reject sufficiently-extreme requests (e.g. pitch=200 or
            // pitch=INT_MIN) before the handler can run, leaving the
            // Tier 1 clamp / log unreachable for those callers and
            // contradicting the tool-description / README claim that
            // out-of-range requests are silently clamped with ESP_LOGW —
            // see Issue #98 (three adversarial-review rounds zeroed in on
            // this exact contract, including the int-boundary corners) and
            // PR #81's defense-in-depth requirement that every servo-write
            // boundary be guarded inside the firmware regardless of
            // caller behavior.
            PropertyList({Property("yaw", kPropertyTypeInteger, 0, -90, 90),
                          Property("pitch", kPropertyTypeInteger, 0,
                                   std::numeric_limits<int>::min(),
                                   std::numeric_limits<int>::max())}),
            [this](const PropertyList& properties) -> ReturnValue {
                int yaw = properties["yaw"].value<int>();
                int pitch = properties["pitch"].value<int>();
                // Issue #80 / #98: two-tier pitch guard.
                //
                // Tier 1 (hard clamp): silently clamp to [SAFE_PITCH_MIN,
                // SAFE_PITCH_MAX] and ESP_LOGW. PitchDegToPos() clamps
                // again at the servo-write boundary (defense-in-depth);
                // doing it here lets us log the original out-of-range
                // value. See the SAFE_PITCH_MIN/MAX comment block above.
                if (pitch < SAFE_PITCH_MIN) {
                    ESP_LOGW(TAG, "set_head_angles: pitch=%d below SAFE_PITCH_MIN=%d, clamping (servo end-stop protection)",
                             pitch, SAFE_PITCH_MIN);
                    pitch = SAFE_PITCH_MIN;
                }
                if (pitch > SAFE_PITCH_MAX) {
                    ESP_LOGW(TAG, "set_head_angles: pitch=%d above SAFE_PITCH_MAX=%d, clamping (servo end-stop protection)",
                             pitch, SAFE_PITCH_MAX);
                    pitch = SAFE_PITCH_MAX;
                }
                // Tier 2 (recommended-range soft signal): inside the hard
                // clamp but outside the M5Stack-documented operating
                // range — accept the value and emit an ESP_LOGI so callers
                // can notice the deviation without blocking the motion.
                if (pitch < RECOMMENDED_PITCH_MIN || pitch > RECOMMENDED_PITCH_MAX) {
                    ESP_LOGI(TAG, "set_head_angles: pitch=%d outside M5Stack-recommended range %d..%d (within hard clamp %d..%d); acceptable but not officially endorsed",
                             pitch, RECOMMENDED_PITCH_MIN, RECOMMENDED_PITCH_MAX,
                             SAFE_PITCH_MIN, SAFE_PITCH_MAX);
                }
                int yaw_pos = YawDegToPos(yaw);
                int pitch_pos = PitchDegToPos(pitch);
                WriteHeadAngles(yaw, pitch);
                bool yaw_motion_started = false;
                bool pitch_motion_started = false;
                if (servo_ok_) {
                    xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                    yaw_motion_started = yaw_motion_.moving;
                    pitch_motion_started = pitch_motion_.moving;
                    xSemaphoreGive(motion_mutex_);
                }
                ESP_LOGI(TAG, "set_head_angles: yaw=%d (pos=%d) motion_started=%d, pitch=%d (pos=%d) motion_started=%d, uart=%d, servo_ok=%d",
                         yaw, yaw_pos, yaw_motion_started, pitch, pitch_pos, pitch_motion_started, (int)SERVO_UART_NUM, servo_ok_);
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "servo_init_ok", servo_ok_);
                cJSON_AddNumberToObject(root, "uart_num", (int)SERVO_UART_NUM);
                cJSON_AddNumberToObject(root, "yaw_pos", yaw_pos);
                cJSON_AddNumberToObject(root, "pitch_pos", pitch_pos);
                cJSON_AddNumberToObject(root, "yaw_motion_started", yaw_motion_started ? 1 : 0);
                cJSON_AddNumberToObject(root, "pitch_motion_started", pitch_motion_started ? 1 : 0);
                return root;
            });

        // Get current head angles
        mcp_server.AddTool(
            "self.robot.get_head_angles",
            "Get the current head angles (yaw, pitch) of the robot in degrees. "
            "Returns {\"yaw\":N,\"pitch\":N} on success; "
            "on persistent ReadPos failure returns "
            "{\"yaw\":null,\"pitch\":null,\"error\":...,\"servo_ok\":bool,"
            "\"yaw_attempts\":N,\"pitch_attempts\":N}.",
            PropertyList(),
            [this](const PropertyList& properties) -> ReturnValue {
                // Issue #123: retry ReadPos a few times before falling back
                // to an explicit error reply. Single-call ReadPos failures
                // (e.g. servo mid-motion, transient bus contention) are a
                // known transient mode that InitializeServo() already treats
                // as warning-and-continue; the previous behaviour of running
                // `ReadPos==-1` through the same `(pos-zero)*5/16` math as a
                // valid position produced sentinel `{-144,-194}` that was
                // indistinguishable from a genuine bus hang at the MCP layer
                // (see #1 / #100 / #118 hang judgments).
                constexpr int kReadPosRetryMax = 3;
                constexpr uint32_t kReadPosRetryDelayMs = 50;

                int yaw_pos = -1;
                int pitch_pos = -1;
                int yaw_attempts = 0;
                int pitch_attempts = 0;
                if (servo_ok_) {
                    xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                    for (int i = 0; i < kReadPosRetryMax; i++) {
                        yaw_attempts = i + 1;
                        yaw_pos = scs_bus_.ReadPos(SERVO_YAW_ID);
                        if (yaw_pos >= 0) break;
                        if (i + 1 < kReadPosRetryMax) {
                            vTaskDelay(pdMS_TO_TICKS(kReadPosRetryDelayMs));
                        }
                    }
                    for (int i = 0; i < kReadPosRetryMax; i++) {
                        pitch_attempts = i + 1;
                        pitch_pos = scs_bus_.ReadPos(SERVO_PITCH_ID);
                        if (pitch_pos >= 0) break;
                        if (i + 1 < kReadPosRetryMax) {
                            vTaskDelay(pdMS_TO_TICKS(kReadPosRetryDelayMs));
                        }
                    }
                    xSemaphoreGive(scs_bus_mutex_);
                }

                cJSON* root = cJSON_CreateObject();
                const bool yaw_ok = yaw_pos >= 0;
                const bool pitch_ok = pitch_pos >= 0;
                if (yaw_ok && pitch_ok) {
                    int yaw = (yaw_pos - 460) * 5 / 16;
                    int pitch = (pitch_pos - 620) * 5 / 16;
                    cJSON_AddNumberToObject(root, "yaw", yaw);
                    cJSON_AddNumberToObject(root, "pitch", pitch);
                } else {
                    cJSON_AddNullToObject(root, "yaw");
                    cJSON_AddNullToObject(root, "pitch");
                    char err[160];
                    snprintf(err, sizeof(err),
                             "ReadPos failed: yaw_raw=%d (attempts=%d) "
                             "pitch_raw=%d (attempts=%d) servo_ok=%d",
                             yaw_pos, yaw_attempts,
                             pitch_pos, pitch_attempts,
                             servo_ok_ ? 1 : 0);
                    cJSON_AddStringToObject(root, "error", err);
                    cJSON_AddBoolToObject(root, "servo_ok", servo_ok_);
                    cJSON_AddNumberToObject(root, "yaw_attempts", yaw_attempts);
                    cJSON_AddNumberToObject(root, "pitch_attempts", pitch_attempts);
                }
                char* str = cJSON_PrintUnformatted(root);
                std::string result(str);
                cJSON_free(str);
                cJSON_Delete(root);
                ESP_LOGI(TAG,
                         "get_head_angles: servo_ok=%d yaw_raw=%d (attempts=%d) "
                         "pitch_raw=%d (attempts=%d) result=%s",
                         servo_ok_ ? 1 : 0,
                         yaw_pos, yaw_attempts,
                         pitch_pos, pitch_attempts,
                         result.c_str());
                return result;
            });

        mcp_server.AddTool(
            "self.robot.set_servo_torque",
            "Enable or disable SCS0009 servo torque on the yaw / pitch axes "
            "independently. Disabling torque stops motor current on that axis; "
            "the head holds via static friction (no motion is commanded). "
            "On disable, the corresponding axis's MotionDriver state is reset "
            "(moving=false, position_unknown=true, request token invalidated) "
            "so a stale interpolation cannot resume on the bus and a "
            "subsequent same-target set_head_angles is re-dispatched rather "
            "than no-op-optimized. Re-enabling torque does NOT trigger a "
            "move -- the next set_head_angles or wobble call will. Returns "
            "the per-axis bus return codes. Diagnostic / power-management "
            "primitive; auto release on idle is tracked separately under "
            "#152 Phase 4.",
            PropertyList({Property("yaw_enabled", kPropertyTypeBoolean),
                          Property("pitch_enabled", kPropertyTypeBoolean)}),
            [this](const PropertyList& properties) -> ReturnValue {
                bool yaw_enabled = properties["yaw_enabled"].value<bool>();
                bool pitch_enabled = properties["pitch_enabled"].value<bool>();
                ServoTorqueResult torque_result = InternalSetServoTorque(
                    yaw_enabled, pitch_enabled, ReleaseReason::kManual);

                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "yaw_enabled", yaw_enabled);
                cJSON_AddBoolToObject(root, "pitch_enabled", pitch_enabled);
                cJSON_AddNumberToObject(root, "yaw_bus_return",
                                        torque_result.yaw_bus_return);
                cJSON_AddNumberToObject(root, "pitch_bus_return",
                                        torque_result.pitch_bus_return);
                cJSON_AddBoolToObject(root, "servo_ok", servo_ok_);
                cJSON_AddBoolToObject(
                    root, "ok",
                    servo_ok_ && (torque_result.short_circuited ||
                                  (torque_result.yaw_ok &&
                                   torque_result.pitch_ok)));
                cJSON_AddBoolToObject(root, "short_circuited",
                                      torque_result.short_circuited);
                if (!servo_ok_) {
                    cJSON_AddStringToObject(root, "error",
                                            "Servo bus not initialized.");
                }
                return root;
            });

        mcp_server.AddTool(
            "self.robot.set_auto_torque_release",
            "Enable or disable automatic SCS0009 torque release after "
            "motion idle timeout. timeout_ms is clamped by the firmware "
            "to 500..600000 ms. Disabling this setting does not re-enable "
            "torque if it is already released; the next set_head_angles, "
            "wobble, or explicit set_servo_torque(true, true) call "
            "re-engages torque.",
            PropertyList({Property("enabled", kPropertyTypeBoolean),
                          Property("timeout_ms", kPropertyTypeInteger,
                                   (int)AUTO_TORQUE_RELEASE_DEFAULT_MS)}),
            [this](const PropertyList& properties) -> ReturnValue {
                bool enabled = properties["enabled"].value<bool>();
                int requested_timeout_ms = properties["timeout_ms"].value<int>();
                bool clamped = false;
                uint32_t timeout_ms = 0;

                if (requested_timeout_ms <
                    static_cast<int>(AUTO_TORQUE_RELEASE_MIN_MS)) {
                    timeout_ms = AUTO_TORQUE_RELEASE_MIN_MS;
                    clamped = true;
                    ESP_LOGW(TAG,
                             "set_auto_torque_release: timeout_ms=%d below "
                             "minimum %u, clamping",
                             requested_timeout_ms,
                             (unsigned)AUTO_TORQUE_RELEASE_MIN_MS);
                } else if (requested_timeout_ms >
                           static_cast<int>(AUTO_TORQUE_RELEASE_MAX_MS)) {
                    timeout_ms = AUTO_TORQUE_RELEASE_MAX_MS;
                    clamped = true;
                    ESP_LOGW(TAG,
                             "set_auto_torque_release: timeout_ms=%d above "
                             "maximum %u, clamping",
                             requested_timeout_ms,
                             (unsigned)AUTO_TORQUE_RELEASE_MAX_MS);
                } else {
                    timeout_ms = static_cast<uint32_t>(requested_timeout_ms);
                }

                bool torque_released_at_call =
                    torque_state_.load(std::memory_order_acquire) ==
                    TorqueState::kReleased;
                auto_release_timeout_ms_.store(timeout_ms,
                                               std::memory_order_release);
                auto_release_enabled_.store(enabled,
                                            std::memory_order_release);

                ESP_LOGI(TAG,
                         "set_auto_torque_release: enabled=%d "
                         "timeout_ms=%u clamped=%d "
                         "torque_released_at_call=%d",
                         enabled ? 1 : 0, (unsigned)timeout_ms,
                         clamped ? 1 : 0,
                         torque_released_at_call ? 1 : 0);

                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "enabled", enabled);
                cJSON_AddNumberToObject(root, "timeout_ms", timeout_ms);
                cJSON_AddBoolToObject(root, "clamped", clamped);
                cJSON_AddBoolToObject(root, "torque_released_at_call",
                                      torque_released_at_call);
                return root;
            });

        // Diagnostic: toggle GPIO6 (servo TX) HIGH/LOW to verify physical signal
        mcp_server.AddTool(
            "self.robot.gpio_test",
            "Diagnostic: toggle GPIO6 (servo TX pin) HIGH/LOW 5 times at 100ms intervals to verify physical signal output. Restores UART pins after.",
            PropertyList(),
            [](const PropertyList& properties) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();

                gpio_num_t pin = static_cast<gpio_num_t>(SERVO_TX_PIN);

                esp_err_t err_dir = gpio_set_direction(pin, GPIO_MODE_OUTPUT);
                cJSON_AddStringToObject(root, "set_direction", esp_err_to_name(err_dir));
                cJSON_AddNumberToObject(root, "pin", SERVO_TX_PIN);

                cJSON* toggles = cJSON_CreateArray();
                for (int i = 0; i < 5; i++) {
                    esp_err_t err_h = gpio_set_level(pin, 1);
                    vTaskDelay(pdMS_TO_TICKS(100));
                    esp_err_t err_l = gpio_set_level(pin, 0);
                    vTaskDelay(pdMS_TO_TICKS(100));
                    cJSON* item = cJSON_CreateObject();
                    cJSON_AddNumberToObject(item, "iter", i);
                    cJSON_AddStringToObject(item, "high", esp_err_to_name(err_h));
                    cJSON_AddStringToObject(item, "low", esp_err_to_name(err_l));
                    cJSON_AddItemToArray(toggles, item);
                }
                cJSON_AddItemToObject(root, "toggles", toggles);

                // Restore UART pin assignment after raw GPIO toggling
                esp_err_t err_restore = uart_set_pin(SERVO_UART_NUM, SERVO_TX_PIN, SERVO_RX_PIN,
                                                    UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
                cJSON_AddStringToObject(root, "uart_pin_restore", esp_err_to_name(err_restore));

                char* str = cJSON_PrintUnformatted(root);
                std::string result(str);
                cJSON_free(str);
                cJSON_Delete(root);
                ESP_LOGI(TAG, "gpio_test: %s", result.c_str());
                return result;
            });

        // Diagnostic: send raw bytes via uart_write_bytes, equivalent to WritePos(1, 1000, 0, 0)
        mcp_server.AddTool(
            "self.robot.uart_diag",
            "Diagnostic: send raw 8 bytes (FF FF 01 04 03 E8 00 00) directly via uart_write_bytes. Returns sent byte count and rx buffer length before/after.",
            PropertyList(),
            [this](const PropertyList& properties) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();

                size_t buf_before = 0;
                esp_err_t err_b = ESP_ERR_INVALID_STATE;
                int written = -1;
                esp_err_t err_wait = ESP_ERR_INVALID_STATE;
                esp_err_t err_a = ESP_ERR_INVALID_STATE;
                size_t buf_after = 0;
                const uint8_t bytes[] = {0xFF, 0xFF, 0x01, 0x04, 0x03, 0xE8, 0x00, 0x00};

                if (servo_ok_) {
                    xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);

                    err_b = uart_get_buffered_data_len(SERVO_UART_NUM, &buf_before);

                    written = uart_write_bytes(SERVO_UART_NUM, (const char*)bytes, sizeof(bytes));

                    // Wait for TX FIFO drain
                    err_wait = uart_wait_tx_done(SERVO_UART_NUM, pdMS_TO_TICKS(100));

                    vTaskDelay(pdMS_TO_TICKS(20));

                    err_a = uart_get_buffered_data_len(SERVO_UART_NUM, &buf_after);

                    xSemaphoreGive(scs_bus_mutex_);
                }
                cJSON_AddStringToObject(root, "buf_before_status", esp_err_to_name(err_b));
                cJSON_AddNumberToObject(root, "buf_before", buf_before);

                cJSON_AddNumberToObject(root, "written", written);
                cJSON_AddNumberToObject(root, "expected", (int)sizeof(bytes));

                cJSON_AddStringToObject(root, "tx_done_status", esp_err_to_name(err_wait));

                cJSON_AddStringToObject(root, "buf_after_status", esp_err_to_name(err_a));
                cJSON_AddNumberToObject(root, "buf_after", buf_after);

                char* str = cJSON_PrintUnformatted(root);
                std::string result(str);
                cJSON_free(str);
                cJSON_Delete(root);
                ESP_LOGI(TAG, "uart_diag: %s", result.c_str());
                return result;
            });

        // Diagnostic: read PY32 REG_GPIO_O_L (output low byte) and report
        // whether VM EN (pin 0) is HIGH. Used to investigate "servo stops
        // moving after the first move_head" — if VM EN drops to LOW under
        // load, the servo loses power even though the I2C write succeeds.
        mcp_server.AddTool(
            "self.robot.check_vm_en",
            "Diagnostic: read PY32 REG_GPIO_O_L and report whether VM EN (pin 0 = servo power) is currently HIGH. "
            "Returns {io_expander_present, i2c_read_ok, raw, vm_en_high}.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                bool present = (io_expander_ != nullptr);
                cJSON_AddBoolToObject(root, "io_expander_present", present);
                if (present) {
                    uint8_t out_low = 0;
                    bool ok = io_expander_->ReadOutputLow(&out_low);
                    cJSON_AddBoolToObject(root, "i2c_read_ok", ok);
                    if (ok) {
                        cJSON_AddNumberToObject(root, "raw", out_low);
                        cJSON_AddBoolToObject(root, "vm_en_high", (out_low & 0x01) != 0);
                    }
                }
                ESP_LOGI(TAG, "check_vm_en queried");
                return root;
            });

        // Set the avatar face (one of: idle, happy, thinking, sad, surprised,
        // embarrassed, off).
        // The image is rendered as a 320x240 overlay on top of the chat UI's
        // emoji_label_ / emoji_image_; LVGL theme/Application emotion updates
        // will keep happening underneath but are visually masked.
        // 'off' hides the avatar lv_obj and disables blink so the underlying
        // xiaozhi-esp32 screens (WiFi config UI, OTA, settings) become visible.
        // A subsequent set_avatar with any other face brings it back, and
        // restores blink to whatever state it was in before going off.
        mcp_server.AddTool(
            "self.display.set_avatar",
            "Set the avatar face displayed on the LCD. face must be one of: "
            "idle, happy, thinking, sad, surprised, embarrassed, off. "
            "'off' hides the avatar and disables blink so the underlying "
            "xiaozhi-esp32 screens (WiFi config UI, OTA, settings) are "
            "visible; calling set_avatar with another face brings the avatar "
            "back and restores the previous blink state.",
            PropertyList({Property("face", kPropertyTypeString)}),
            [this](const PropertyList& properties) -> ReturnValue {
                std::string face = properties["face"].value<std::string>();
                cJSON* root = cJSON_CreateObject();
                cJSON_AddStringToObject(root, "face", face.c_str());

                bool applied = false;
                if (face == "off") {
                    // Any avatar transition supersedes an in-flight mouth
                    // sequence (per Issue #5 acceptance: "set_avatar() takes
                    // effect after the queued sequence finishes (or
                    // interrupts cleanly)").
                    RequestMouthSequenceCancel();
                    applied = SetAvatarOff();
                } else if (AvatarImageFor(face.c_str()) != nullptr) {
                    RequestMouthSequenceCancel();
                    applied = SetAvatarExpression(face.c_str());
                } else {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error",
                        "Unknown face. Allowed: idle, happy, thinking, sad, "
                        "surprised, embarrassed, off.");
                    ESP_LOGW(TAG, "set_avatar rejected: unknown face '%s'", face.c_str());
                    return root;
                }
                cJSON_AddBoolToObject(root, "ok", applied);
                if (!applied) {
                    cJSON_AddStringToObject(root, "error",
                        "Display not ready yet; retry after a moment.");
                }
                ESP_LOGI(TAG, "set_avatar: face=%s applied=%d", face.c_str(), applied);
                return root;
            });

        // Phase 2: lip-sync. Swap the avatar to one of the mouth-only frames.
        // The shape is held until the next set_avatar / set_mouth / blink, so
        // callers should drive it from their TTS / audio level loop.
        mcp_server.AddTool(
            "self.display.set_mouth",
            "Set the avatar mouth shape. mouth must be one of: "
            "closed, half, open, e, u. Held until the next set_avatar/set_mouth, "
            "or until a blink restores the resting face.",
            PropertyList({Property("mouth", kPropertyTypeString)}),
            [this](const PropertyList& properties) -> ReturnValue {
                std::string mouth = properties["mouth"].value<std::string>();
                bool valid = (MouthImageFor(mouth.c_str()) != nullptr);
                cJSON* root = cJSON_CreateObject();
                cJSON_AddStringToObject(root, "mouth", mouth.c_str());
                if (!valid) {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error",
                        "Unknown mouth. Allowed: closed, half, open, e, u.");
                    ESP_LOGW(TAG, "set_mouth rejected: unknown shape '%s'", mouth.c_str());
                    return root;
                }
                // Any explicit mouth set supersedes an in-flight sequence
                // (Issue #5: set_mouth("closed") doubles as the cancellation
                // path so we don't need a separate cancel_mouth_sequence
                // tool).
                RequestMouthSequenceCancel();
                bool applied = SetMouthShape(mouth.c_str());
                cJSON_AddBoolToObject(root, "ok", applied);
                if (!applied) {
                    cJSON_AddStringToObject(root, "error",
                        "Display not ready yet; retry after a moment.");
                }
                ESP_LOGI(TAG, "set_mouth: mouth=%s applied=%d", mouth.c_str(), applied);
                return root;
            });

        // Phase 2: lip-sync sequence. Queue and play a list of
        // {shape, duration_ms} pairs locally so a TTS-driven caller can
        // ship one MCP call per utterance instead of N back-to-back
        // set_mouth calls (which suffer per-step WebSocket RTT jitter).
        // The MCP Property type system has no array kind, so the gateway
        // serialises `steps` to a JSON string and sends it as `steps_json`.
        // See Phase 2 lip-sync sequence playback comment block above for
        // the concurrency model and trade-offs (blink pause, atomic queue
        // replacement, final shape held).
        mcp_server.AddTool(
            "self.display.set_mouth_sequence",
            "Queue a lip-sync sequence and play it locally. steps_json must "
            "decode to a JSON array of {shape, duration_ms} objects (1..256 "
            "items, shape in {closed, half, open, e, u}, duration_ms in "
            "10..10000). Returns immediately; calling set_mouth, set_avatar, "
            "or this tool again interrupts the in-flight sequence. "
            "Autonomous blink is paused while a sequence plays and resumed "
            "when it ends (resume reads the user's most recent set_blink "
            "intent, not a snapshot). The final shape is held until the "
            "next set_mouth / set_avatar call, or until the next autonomous "
            "blink restores the resting face — the same Phase 2 trade-off "
            "that applies to set_mouth, since blink ends by repainting the "
            "full face. If the final shape must persist visually, disable "
            "blink with set_blink(false) before the sequence (or append a "
            "closed step if you just want the mouth to close at the end).",
            PropertyList({Property("steps_json", kPropertyTypeString)}),
            [this](const PropertyList& properties) -> ReturnValue {
                std::string steps_json = properties["steps_json"].value<std::string>();
                MouthSequenceEnqueueResult r = EnqueueMouthSequence(steps_json);
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "ok", r.ok);
                if (!r.ok) {
                    cJSON_AddStringToObject(root, "error", r.error.c_str());
                    ESP_LOGW(TAG, "set_mouth_sequence rejected: %s", r.error.c_str());
                } else {
                    cJSON_AddNumberToObject(root, "queued_steps", r.queued_steps);
                    cJSON_AddNumberToObject(root, "estimated_duration_ms",
                                            static_cast<double>(r.total_duration_ms));
                    ESP_LOGI(TAG, "set_mouth_sequence queued %d steps (%u ms)",
                             r.queued_steps, (unsigned)r.total_duration_ms);
                }
                return root;
            });

        // Phase 2: enable/disable autonomous blinking. When enabled, a
        // background timer fires every 3-6 s (random) and runs the four-step
        // blink sequence (half -> closed -> half -> face). Also captures
        // the user's intent into blink_desired_ so that a call issued while
        // a mouth sequence is suppressing blink is honoured when the
        // sequence finishes.
        mcp_server.AddTool(
            "self.display.set_blink",
            "Enable or disable autonomous eye blinking on the avatar. "
            "When enabled, a brief blink animation runs every 3-6 seconds. "
            "If a set_mouth_sequence is currently playing, blink is paused "
            "until the sequence ends; this call still records the intent "
            "and is applied at the sequence end (or immediately if no "
            "sequence is running).",
            PropertyList({Property("enabled", kPropertyTypeBoolean)}),
            [this](const PropertyList& properties) -> ReturnValue {
                bool enabled = properties["enabled"].value<bool>();
                blink_desired_.store(enabled, std::memory_order_release);
                bool deferred = mouth_seq_active_.load(std::memory_order_acquire);
                if (!deferred) {
                    if (enabled) {
                        StartBlinkTimer();
                    } else {
                        StopBlinkTimer();
                    }
                }
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "enabled", enabled);
                cJSON_AddBoolToObject(root, "ok", true);
                if (deferred) {
                    cJSON_AddBoolToObject(root, "deferred", true);
                }
                ESP_LOGI(TAG, "set_blink: enabled=%d deferred=%d",
                         (int)enabled, deferred ? 1 : 0);
                return root;
            });

        // Phase 7: head-touch (Si12T). Returns the latest debounced zone
        // states plus the most recent gesture event. Polled by the MCP client
        // to notice TAP/STROKE on the head without holding open a stream.
        mcp_server.AddTool(
            "self.touch.get_touch_state",
            "Get the current head-touch sensor state and last gesture event "
            "(tap/stroke/idle) with its age in milliseconds.",
            PropertyList(),
            [this](const PropertyList& properties) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "available", si12t_ok_);
                cJSON_AddBoolToObject(root, "zone0", last_zone_snapshot_[0]);
                cJSON_AddBoolToObject(root, "zone1", last_zone_snapshot_[1]);
                cJSON_AddBoolToObject(root, "zone2", last_zone_snapshot_[2]);
                cJSON_AddNumberToObject(root, "raw", last_output1_raw_);
                const char* ev = "idle";
                switch (last_event_) {
                    case TouchEvent::TAP:    ev = "tap";    break;
                    case TouchEvent::STROKE: ev = "stroke"; break;
                    case TouchEvent::IDLE:
                    default:                 ev = "idle";   break;
                }
                cJSON_AddStringToObject(root, "last_event", ev);
                int64_t age_ms = -1;
                if (last_event_us_ != 0) {
                    int64_t now_us = (int64_t)esp_timer_get_time();
                    age_ms = (now_us - (int64_t)last_event_us_) / 1000;
                    if (age_ms < 0) age_ms = 0;
                }
                cJSON_AddNumberToObject(root, "last_event_age_ms", (double)age_ms);
                return root;
            });

        // ---- LED tools (12x WS2812C on the StackChan base) ----
        // The strip is driven by the PY32 IO expander on its pin 13, not by
        // an ESP32 GPIO. Updates are non-latching writes into the PY32 LED
        // RAM followed by a single RefreshLeds() to strobe the strip. All
        // four tools refresh implicitly so the LLM gets WYSIWYG behaviour.
        mcp_server.AddTool(
            "self.led.set_color",
            "Set a single RGB LED on the StackChan base. There are 12 LEDs "
            "(index 0..11). r/g/b are 0..255. Updates immediately.",
            PropertyList({
                Property("index", kPropertyTypeInteger, 0, RGB_LED_COUNT - 1),
                Property("r", kPropertyTypeInteger, 0, 255),
                Property("g", kPropertyTypeInteger, 0, 255),
                Property("b", kPropertyTypeInteger, 0, 255),
            }),
            [this](const PropertyList& properties) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "available", rgb_ok_);
                if (!rgb_ok_) {
                    cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                    return root;
                }
                int index = properties["index"].value<int>();
                uint8_t r = ClampByte(properties["r"].value<int>());
                uint8_t g = ClampByte(properties["g"].value<int>());
                uint8_t b = ClampByte(properties["b"].value<int>());
                bool ok_w = io_expander_->SetLedColor((uint8_t)index, r, g, b);
                bool ok_r = ok_w ? io_expander_->RefreshLeds() : false;
                cJSON_AddBoolToObject(root, "ok", ok_w && ok_r);
                cJSON_AddNumberToObject(root, "index", index);
                ESP_LOGI(TAG, "set_led: index=%d rgb=(%u,%u,%u) ok=%d", index, r, g, b, ok_w && ok_r);
                return root;
            });

        mcp_server.AddTool(
            "self.led.set_all",
            "Set all 12 RGB LEDs on the StackChan base to the same color. "
            "r/g/b are 0..255. Updates immediately.",
            PropertyList({
                Property("r", kPropertyTypeInteger, 0, 255),
                Property("g", kPropertyTypeInteger, 0, 255),
                Property("b", kPropertyTypeInteger, 0, 255),
            }),
            [this](const PropertyList& properties) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "available", rgb_ok_);
                if (!rgb_ok_) {
                    cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                    return root;
                }
                uint8_t r = ClampByte(properties["r"].value<int>());
                uint8_t g = ClampByte(properties["g"].value<int>());
                uint8_t b = ClampByte(properties["b"].value<int>());
                uint8_t buf[RGB_LED_COUNT * 2];
                uint8_t pair[2];
                PackRgb565(r, g, b, pair);
                for (int i = 0; i < RGB_LED_COUNT; i++) {
                    buf[i * 2 + 0] = pair[0];
                    buf[i * 2 + 1] = pair[1];
                }
                bool ok_w = io_expander_->SetLedData(buf, sizeof(buf));
                bool ok_r = ok_w ? io_expander_->RefreshLeds() : false;
                cJSON_AddBoolToObject(root, "ok", ok_w && ok_r);
                ESP_LOGI(TAG, "set_all_leds: rgb=(%u,%u,%u) ok=%d", r, g, b, ok_w && ok_r);
                return root;
            });

        // Batch set: accepts a JSON-encoded array of 12 [r,g,b] triples.
        // Single I2C burst + one refresh — use this for animations or any
        // multi-color pattern to avoid 12x round-trips. Missing trailing
        // entries are left at their previous color (PY32 RAM is sticky).
        mcp_server.AddTool(
            "self.led.set_many",
            "Set multiple RGB LEDs in one shot. 'colors' is a JSON-encoded "
            "array of [r,g,b] triples starting at index 0, e.g. "
            "\"[[255,0,0],[0,255,0],[0,0,255]]\". Up to 12 entries; extras "
            "are ignored, missing entries keep their previous color. "
            "r/g/b are 0..255. Updates immediately.",
            PropertyList({Property("colors", kPropertyTypeString)}),
            [this](const PropertyList& properties) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "available", rgb_ok_);
                if (!rgb_ok_) {
                    cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                    return root;
                }
                std::string json = properties["colors"].value<std::string>();
                cJSON* arr = cJSON_Parse(json.c_str());
                if (arr == nullptr || !cJSON_IsArray(arr)) {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error",
                        "colors must be a JSON array of [r,g,b] triples");
                    if (arr != nullptr) cJSON_Delete(arr);
                    return root;
                }
                int n = cJSON_GetArraySize(arr);
                if (n > RGB_LED_COUNT) n = RGB_LED_COUNT;

                // Validate every entry FIRST and pack into a local buffer.
                // Only after the whole array is known good do we touch the
                // PY32 — that way a malformed entry at i=5 cannot leave
                // LEDs 0..4 mutated (atomic semantics, same as
                // set_mouth_sequence). cJSON_IsNumber is required because
                // valueint silently returns 0 for non-number nodes (string,
                // null, bool), so without the guard a payload like
                // [["255",0,0]] would write black and report ok=true.
                uint8_t buf[RGB_LED_COUNT * 2];   // 24 bytes, fits the cap
                bool parse_ok = true;
                for (int i = 0; i < n; i++) {
                    cJSON* triple = cJSON_GetArrayItem(arr, i);
                    if (!cJSON_IsArray(triple) || cJSON_GetArraySize(triple) < 3) {
                        parse_ok = false;
                        break;
                    }
                    cJSON* jr = cJSON_GetArrayItem(triple, 0);
                    cJSON* jg = cJSON_GetArrayItem(triple, 1);
                    cJSON* jb = cJSON_GetArrayItem(triple, 2);
                    if (!cJSON_IsNumber(jr) || !cJSON_IsNumber(jg) || !cJSON_IsNumber(jb)) {
                        parse_ok = false;
                        break;
                    }
                    PackRgb565(ClampByte(jr->valueint),
                               ClampByte(jg->valueint),
                               ClampByte(jb->valueint),
                               &buf[i * 2]);
                }
                cJSON_Delete(arr);

                // Single I2C burst for the validated prefix, then one latch.
                // n=0 is treated as success (gateway schema enforces
                // minItems=1, but a direct device caller could hit this).
                bool ok_w = false, ok_r = false;
                if (parse_ok && n > 0) {
                    ok_w = io_expander_->SetLedData(buf, (size_t)(n * 2));
                    ok_r = ok_w ? io_expander_->RefreshLeds() : false;
                }
                bool ok = parse_ok && (n == 0 || (ok_w && ok_r));
                cJSON_AddBoolToObject(root, "ok", ok);
                cJSON_AddNumberToObject(root, "written", ok ? n : 0);
                if (!parse_ok) {
                    cJSON_AddStringToObject(root, "error",
                        "Each entry must be a [r,g,b] triple of integers");
                }
                ESP_LOGI(TAG, "set_many_leds: written=%d/%d ok=%d",
                         ok ? n : 0, n, ok);
                return root;
            });

        mcp_server.AddTool(
            "self.led.clear",
            "Turn off all 12 RGB LEDs on the StackChan base. Updates immediately.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                cJSON_AddBoolToObject(root, "available", rgb_ok_);
                if (!rgb_ok_) {
                    cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                    return root;
                }
                uint8_t buf[RGB_LED_COUNT * 2] = {0};
                bool ok_w = io_expander_->SetLedData(buf, sizeof(buf));
                bool ok_r = ok_w ? io_expander_->RefreshLeds() : false;
                cJSON_AddBoolToObject(root, "ok", ok_w && ok_r);
                ESP_LOGI(TAG, "clear_leds: ok=%d", ok_w && ok_r);
                return root;
            });

        // ---- Generic I2C bus tools (Grove Port A) ----
        // Expose the external Port A I2C bus to the MCP client so that
        // attached M5Stack Unit modules (ENV III, ToF, gas sensor, PaHub,
        // etc.) can be driven from the gateway / host side without
        // recompiling and re-flashing per Unit. The on-board IC bus (PMIC,
        // touch, IMU, AW9523, audio codec) is on a physically separate I2C
        // controller and is NOT reachable from these tools by construction.

        mcp_server.AddTool(
            "self.i2c.scan",
            "Scan the external I2C bus on Grove Port A and return all 7-bit "
            "addresses (probe range 0x08..0x77, excluding I2C reserved "
            "ranges) that ACK a probe. Use this to discover attached "
            "M5Stack Unit modules (ENV III, ToF, gas sensor, PaHub, etc.). "
            "On-board ICs on the internal bus are NOT included (this tool "
            "operates on a physically separate bus). Returns "
            "{\"ok\":true, \"addresses\":[...]}.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                cJSON* addrs = cJSON_CreateArray();
                int found = 0;
                // Probe 0x08..0x77 (skip I2C reserved 0x00-0x07 / 0x78-0x7F).
                // 200 ms per-probe timeout matches the boot-time I2cDetect()
                // and reliably catches slower Units (RCWL-9620 etc.).
                for (uint8_t addr = 0x08; addr < 0x78; addr++) {
                    esp_err_t ret = i2c_master_probe(port_a_i2c_bus_, addr, pdMS_TO_TICKS(200));
                    if (ret == ESP_OK) {
                        cJSON_AddItemToArray(addrs, cJSON_CreateNumber(addr));
                        found++;
                    }
                }
                cJSON_AddBoolToObject(root, "ok", true);
                cJSON_AddItemToObject(root, "addresses", addrs);
                ESP_LOGI(TAG, "i2c.scan: found %d device(s) on Port A", found);
                return root;
            });

        mcp_server.AddTool(
            "self.i2c.read",
            "Read n_bytes from an I2C device at 7-bit address `addr` on Grove "
            "Port A. `addr` is restricted to 0x08..0x77 (I2C reserved ranges "
            "excluded — matches the self.i2c.scan probe range). Use this for "
            "protocols that read the device's current register / output "
            "without a preceding write (e.g. sensors that latch a measurement "
            "from a prior command). For typical 'write register address, "
            "then read' patterns, use self.i2c.write_read instead. Returns "
            "{\"ok\":true, \"bytes\":[...]} or "
            "{\"ok\":false, \"error\":\"ESP_ERR_TIMEOUT\"} on NACK.",
            PropertyList({
                Property("addr", kPropertyTypeInteger, 0x08, 0x77),
                Property("n_bytes", kPropertyTypeInteger, 1, 256)
            }),
            [this](const PropertyList& props) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                uint8_t addr = static_cast<uint8_t>(props["addr"].value<int>());
                int n = props["n_bytes"].value<int>();

                i2c_device_config_t cfg = {
                    .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                    .device_address = addr,
                    .scl_speed_hz = 400000,
                };
                i2c_master_dev_handle_t dev;
                esp_err_t err = i2c_master_bus_add_device(port_a_i2c_bus_, &cfg, &dev);
                if (err != ESP_OK) {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                    ESP_LOGW(TAG, "i2c.read addr=0x%02X add_device failed: %s",
                             addr, esp_err_to_name(err));
                    return root;
                }

                std::vector<uint8_t> buf(static_cast<size_t>(n));
                err = i2c_master_receive(dev, buf.data(), buf.size(), 100);
                i2c_master_bus_rm_device(dev);

                if (err == ESP_OK) {
                    cJSON* bytes = cJSON_CreateArray();
                    for (uint8_t b : buf) {
                        cJSON_AddItemToArray(bytes, cJSON_CreateNumber(b));
                    }
                    cJSON_AddBoolToObject(root, "ok", true);
                    cJSON_AddItemToObject(root, "bytes", bytes);
                } else {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                }
                ESP_LOGI(TAG, "i2c.read addr=0x%02X n=%d ok=%d",
                         addr, n, err == ESP_OK);
                return root;
            });

        Property i2c_write_bytes_prop(
            "bytes", kPropertyTypeArray, kPropertyElementTypeInteger, 0, 255
        );
        i2c_write_bytes_prop.set_max_items(256);  // 対称: n_bytes の read 上限と同じ
        mcp_server.AddTool(
            "self.i2c.write",
            "Write bytes to an I2C device at 7-bit address `addr` on Grove "
            "Port A. `addr` is restricted to 0x08..0x77 (I2C reserved ranges "
            "excluded — General-call address 0x00 etc. cannot accidentally "
            "broadcast-write to all attached Units). `bytes` is an array of "
            "integers (0..255, max 256 items). This tool operates on the "
            "external Port A bus only; on-board ICs (PMIC, AW9523, touch, "
            "etc.) on the internal bus are not reachable. Returns "
            "{\"ok\":true} on ACK or "
            "{\"ok\":false, \"error\":\"ESP_ERR_TIMEOUT\"} on NACK.",
            PropertyList({
                Property("addr", kPropertyTypeInteger, 0x08, 0x77),
                i2c_write_bytes_prop
            }),
            [this](const PropertyList& props) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                uint8_t addr = static_cast<uint8_t>(props["addr"].value<int>());
                auto bytes_int = props["bytes"].value<std::vector<int>>();

                i2c_device_config_t cfg = {
                    .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                    .device_address = addr,
                    .scl_speed_hz = 400000,
                };
                i2c_master_dev_handle_t dev;
                esp_err_t err = i2c_master_bus_add_device(port_a_i2c_bus_, &cfg, &dev);
                if (err != ESP_OK) {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                    ESP_LOGW(TAG, "i2c.write addr=0x%02X add_device failed: %s",
                             addr, esp_err_to_name(err));
                    return root;
                }

                std::vector<uint8_t> buf;
                buf.reserve(bytes_int.size());
                for (int b : bytes_int) buf.push_back(static_cast<uint8_t>(b));

                err = i2c_master_transmit(dev, buf.data(), buf.size(), 100);
                i2c_master_bus_rm_device(dev);

                if (err == ESP_OK) {
                    cJSON_AddBoolToObject(root, "ok", true);
                } else {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                }
                ESP_LOGI(TAG, "i2c.write addr=0x%02X n=%d ok=%d",
                         addr, (int)buf.size(), err == ESP_OK);
                return root;
            });

        Property i2c_wr_write_bytes_prop(
            "write_bytes", kPropertyTypeArray, kPropertyElementTypeInteger, 0, 255
        );
        i2c_wr_write_bytes_prop.set_max_items(256);  // 対称: n_bytes の read 上限と同じ
        mcp_server.AddTool(
            "self.i2c.write_read",
            "Write `write_bytes` to an I2C device at 7-bit address `addr` on "
            "Grove Port A, then read n_bytes back in a single transaction "
            "(Repeated Start). `addr` is restricted to 0x08..0x77 (I2C "
            "reserved ranges excluded). `write_bytes` is an array of "
            "integers (0..255, max 256 items). This is the common 'set "
            "register pointer, then read' pattern: pass write_bytes=[reg_addr] "
            "to read from a specific register. Returns "
            "{\"ok\":true, \"bytes\":[...]} or "
            "{\"ok\":false, \"error\":\"...\"} on failure.",
            PropertyList({
                Property("addr", kPropertyTypeInteger, 0x08, 0x77),
                i2c_wr_write_bytes_prop,
                Property("n_bytes", kPropertyTypeInteger, 1, 256)
            }),
            [this](const PropertyList& props) -> ReturnValue {
                cJSON* root = cJSON_CreateObject();
                uint8_t addr = static_cast<uint8_t>(props["addr"].value<int>());
                auto write_bytes_int = props["write_bytes"].value<std::vector<int>>();
                int n = props["n_bytes"].value<int>();

                i2c_device_config_t cfg = {
                    .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                    .device_address = addr,
                    .scl_speed_hz = 400000,
                };
                i2c_master_dev_handle_t dev;
                esp_err_t err = i2c_master_bus_add_device(port_a_i2c_bus_, &cfg, &dev);
                if (err != ESP_OK) {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                    ESP_LOGW(TAG, "i2c.write_read addr=0x%02X add_device failed: %s",
                             addr, esp_err_to_name(err));
                    return root;
                }

                std::vector<uint8_t> write_buf;
                write_buf.reserve(write_bytes_int.size());
                for (int b : write_bytes_int) write_buf.push_back(static_cast<uint8_t>(b));

                std::vector<uint8_t> read_buf(static_cast<size_t>(n));
                err = i2c_master_transmit_receive(dev,
                                                   write_buf.data(), write_buf.size(),
                                                   read_buf.data(), read_buf.size(),
                                                   100);
                i2c_master_bus_rm_device(dev);

                if (err == ESP_OK) {
                    cJSON* bytes = cJSON_CreateArray();
                    for (uint8_t b : read_buf) {
                        cJSON_AddItemToArray(bytes, cJSON_CreateNumber(b));
                    }
                    cJSON_AddBoolToObject(root, "ok", true);
                    cJSON_AddItemToObject(root, "bytes", bytes);
                } else {
                    cJSON_AddBoolToObject(root, "ok", false);
                    cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                }
                ESP_LOGI(TAG, "i2c.write_read addr=0x%02X w=%d r=%d ok=%d",
                         addr, (int)write_buf.size(), n, err == ESP_OK);
                return root;
            });

        ESP_LOGI(TAG, "StackChan MCP tools registered");
    }

public:
    StackChanBoard() {
        InitializePowerSaveTimer();
        InitializeI2c();
        InitializePortAI2c();
        InitializeAxp2101();
        InitializeAw9523();
        // I2cDetect() moved AFTER all I2C device initializations.
        // The 128-address probe (i2c_master_probe over the whole bus) was
        // leaving PY32 (0x6F) in a half-finished slave state, so the
        // following transmit_receive (REG_VERSION via Repeated Start)
        // timed out (0x103). Doing the scan after IOExpander/Si12T init
        // preserves the boot-log debug info without poisoning subsequent
        // register reads. Si12T (0x68) is unaffected on the same bus,
        // but moving the scan is safer for any future I2C peripheral too.
        InitializeSpi();
        InitializeIli9342Display();
        InitializeCamera();
        InitializeFt6336TouchPad();
        GetBacklight()->RestoreBrightness();
        InitializeIOExpander();
        InitializeServo();
        InitializeSi12tTouch();
        I2cDetect();
        // Avatar auto-display disabled: WiFi config UI needs to be visible.
        // Avatar is shown on-demand via MCP set_avatar command.
        // InitializeAvatar();
        InitializeMouthSequenceTask();
        RegisterMcpTools();
    }

    virtual AudioCodec* GetAudioCodec() override {
        static CoreS3AudioCodec audio_codec(i2c_bus_,
            AUDIO_INPUT_SAMPLE_RATE,
            AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_MCLK,
            AUDIO_I2S_GPIO_BCLK,
            AUDIO_I2S_GPIO_WS,
            AUDIO_I2S_GPIO_DOUT,
            AUDIO_I2S_GPIO_DIN,
            AUDIO_CODEC_AW88298_ADDR,
            AUDIO_CODEC_ES7210_ADDR,
            AUDIO_INPUT_REFERENCE);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }

    virtual Camera* GetCamera() override {
        return camera_;
    }

    virtual bool GetBatteryLevel(int &level, bool& charging, bool& discharging) override {
        static bool last_discharging = false;
        charging = pmic_->IsCharging();
        discharging = pmic_->IsDischarging();
        if (discharging != last_discharging) {
            power_save_timer_->SetEnabled(discharging);
            last_discharging = discharging;
        }

        level = pmic_->GetBatteryLevel();
        return true;
    }

    virtual void SetPowerSaveLevel(PowerSaveLevel level) override {
        if (level != PowerSaveLevel::LOW_POWER) {
            power_save_timer_->WakeUp();
        }
        WifiBoard::SetPowerSaveLevel(level);
    }

    // Phase 4 audio (Issue #76): drive avatar mouth animation alongside TTS
    // playback. The gateway's tts.start / tts.stop notifications reach this
    // board via Application::OnIncomingJson() -> Board::OnTtsStart/Stop().
    virtual void OnTtsStart() override {
        StartTtsLipSync();
    }

    virtual void OnTtsStop() override {
        StopTtsLipSync();
    }

    virtual Backlight *GetBacklight() override {
        static CustomBacklight backlight(pmic_);
        return &backlight;
    }
};

DECLARE_BOARD(StackChanBoard);
