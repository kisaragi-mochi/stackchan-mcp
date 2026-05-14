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
#include <atomic>
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
    i2c_master_bus_handle_t i2c_bus_;
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
    esp_timer_handle_t servo_wobble_timer_ = nullptr;

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
    int servo_wobble_step_ = 0;       // 0..3 sequence index
    bool servo_wobble_active_ = false;

    // Issue #1: Servo motion task — interpolate WritePos to avoid SCS0009 bus
    // collisions on large-angle reversals. A dedicated FreeRTOS task ticks at
    // MOTION_TICK_MS, walks current_deg → target_deg over move_duration_ms, and
    // issues short MOTION_PER_WRITE_TIME_MS WritePos commands so the servo never
    // receives a discontinuous jump.
    struct AxisMotion {
        int target_deg = 0;
        int start_deg = 0;
        int current_deg = 0;
        uint32_t move_start_ms = 0;
        uint32_t move_duration_ms = 0;
        bool moving = false;
    };
    // TODO: motion_mutex_/scs_bus_mutex_/servo_task_handle_ have no destroy path; board is singleton via DECLARE_BOARD.
    AxisMotion yaw_motion_;
    AxisMotion pitch_motion_;
    SemaphoreHandle_t motion_mutex_ = nullptr;     // protects AxisMotion fields
    SemaphoreHandle_t scs_bus_mutex_ = nullptr;    // serializes UART access (WritePos/ReadPos)
    TaskHandle_t servo_task_handle_ = nullptr;
    static constexpr uint32_t MOTION_TICK_MS = 20;
    static constexpr uint32_t MOTION_DEFAULT_DURATION_MS = 600;
    static constexpr uint32_t MOTION_PER_WRITE_TIME_MS = 30;

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

            // Issue #123: log boot-init pre-WritePos position with tick so
            // the "unintended downward drop on power-on" (#121 hypothesis 1/2)
            // becomes data-driven. ServoTask has not been created yet, so no
            // scs_bus_mutex_ contention is possible at this point.
            int yaw_pos_actual = scs_bus_.ReadPos(SERVO_YAW_ID);
            int pitch_pos_actual = scs_bus_.ReadPos(SERVO_PITCH_ID);
            ESP_LOGI(TAG,
                     "Boot pre-init ReadPos: yaw_raw=%d pitch_raw=%d tick=%u",
                     yaw_pos_actual, pitch_pos_actual,
                     (unsigned)xTaskGetTickCount());
            if (yaw_pos_actual >= 0) {
                yaw_motion_.current_deg = (yaw_pos_actual - 460) * 5 / 16;
                ESP_LOGI(TAG, "Restored yaw_motion_.current_deg=%d from ReadPos=%d",
                         yaw_motion_.current_deg, yaw_pos_actual);
            } else {
                ESP_LOGW(TAG, "Failed to ReadPos(yaw); current_deg stays at 0");
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
                ESP_LOGW(TAG, "Failed to ReadPos(pitch); current_deg stays at 0");
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
            constexpr int BOOT_INIT_YAW_DEG = 0;
            constexpr int BOOT_INIT_PITCH_DEG = 45;
            // Issue #121: at the original 1000 ms duration the
            // post-power-on climb to 45° produced ~45°/s angular speed,
            // perceived on-device as startling ("ブルンっ" + audible
            // stress). Bumping the interpolated move to 4000 ms drops
            // the effective rate to ~11°/s while keeping the motion
            // monotonic through the existing `WriteHeadAngles` ->
            // `servo_motion` task path. The 100 ms post-settle margin
            // is unchanged. This is the cheap fix for #121 Problem 2;
            // the separate "unintended downward drop on power-on"
            // (#121 Problem 1 / hypotheses 1-3) still requires a
            // root-cause investigation tracked separately.
            constexpr uint32_t BOOT_INIT_MOVE_MS = 4000;
            TickType_t boot_init_start_tick = xTaskGetTickCount();
            WriteHeadAngles(BOOT_INIT_YAW_DEG, BOOT_INIT_PITCH_DEG, BOOT_INIT_MOVE_MS);
            vTaskDelay(pdMS_TO_TICKS(BOOT_INIT_MOVE_MS + 100));

            // Issue #123: capture post-init ReadPos so the boot-init effect
            // is observable in the serial log. ServoTask is now running, so
            // hold scs_bus_mutex_ across the ReadPos pair.
            int post_yaw_pos = -1;
            int post_pitch_pos = -1;
            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
            post_yaw_pos = scs_bus_.ReadPos(SERVO_YAW_ID);
            post_pitch_pos = scs_bus_.ReadPos(SERVO_PITCH_ID);
            xSemaphoreGive(scs_bus_mutex_);
            TickType_t boot_init_end_tick = xTaskGetTickCount();
            ESP_LOGI(TAG,
                     "Boot-time servo init complete: target yaw=%d pitch=%d "
                     "(move=%ums), post-ReadPos: yaw_raw=%d pitch_raw=%d, "
                     "elapsed_ms=%u",
                     BOOT_INIT_YAW_DEG, BOOT_INIT_PITCH_DEG,
                     (unsigned)BOOT_INIT_MOVE_MS,
                     post_yaw_pos, post_pitch_pos,
                     (unsigned)((boot_init_end_tick - boot_init_start_tick) *
                                portTICK_PERIOD_MS));
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
    void WriteHeadAngles(int yaw_deg, int pitch_deg,
                         uint32_t duration_ms = MOTION_DEFAULT_DURATION_MS) {
        if (!servo_ok_) {
            ESP_LOGW(TAG, "WriteHeadAngles skipped: servo not initialized");
            return;
        }
        uint32_t now_ms = static_cast<uint32_t>(esp_timer_get_time() / 1000);

        xSemaphoreTake(motion_mutex_, portMAX_DELAY);
        yaw_motion_.target_deg = yaw_deg;
        yaw_motion_.start_deg = yaw_motion_.current_deg;
        yaw_motion_.move_start_ms = now_ms;
        yaw_motion_.move_duration_ms = duration_ms;
        yaw_motion_.moving = (yaw_motion_.target_deg != yaw_motion_.current_deg);

        pitch_motion_.target_deg = pitch_deg;
        pitch_motion_.start_deg = pitch_motion_.current_deg;
        pitch_motion_.move_start_ms = now_ms;
        pitch_motion_.move_duration_ms = duration_ms;
        pitch_motion_.moving = (pitch_motion_.target_deg != pitch_motion_.current_deg);
        xSemaphoreGive(motion_mutex_);
    }

    // Servo wobble: yaw -A -> +A -> -A -> 0, each step SERVO_WOBBLE_STEP_MS.
    // Driven by servo_wobble_timer_ to avoid blocking the touch poll task.
    static void ServoWobbleStepCb(void* arg) {
        StackChanBoard* self = static_cast<StackChanBoard*>(arg);
        self->ServoWobbleStepAdvance();
    }

    void ServoWobbleStepAdvance() {
        const int A = SERVO_WOBBLE_AMPLITUDE_DEG;
        switch (servo_wobble_step_) {
            case 0: WriteHeadAngles(-A, 0, SERVO_WOBBLE_STEP_MS); break;
            case 1: WriteHeadAngles(+A, 0, SERVO_WOBBLE_STEP_MS); break;
            case 2: WriteHeadAngles(-A, 0, SERVO_WOBBLE_STEP_MS); break;
            case 3: WriteHeadAngles(  0, 0, SERVO_WOBBLE_STEP_MS); break;
            default:
                servo_wobble_active_ = false;
                return;
        }
        servo_wobble_step_++;
        if (servo_wobble_step_ <= 3) {
            esp_timer_start_once(servo_wobble_timer_,
                                 (uint64_t)SERVO_WOBBLE_STEP_MS * 1000);
        } else {
            servo_wobble_active_ = false;
        }
    }

    void StartServoWobble() {
        if (!servo_ok_) {
            ESP_LOGW(TAG, "Servo wobble skipped: servo not initialized");
            return;
        }
        if (servo_wobble_active_) {
            // Restart from step 0 if a new wobble is requested mid-flight.
            esp_timer_stop(servo_wobble_timer_);
        }
        if (servo_wobble_timer_ == nullptr) {
            esp_timer_create_args_t args = {
                .callback = &StackChanBoard::ServoWobbleStepCb,
                .arg = this,
                .dispatch_method = ESP_TIMER_TASK,
                .name = "servo_wobble",
                .skip_unhandled_events = true,
            };
            ESP_ERROR_CHECK(esp_timer_create(&args, &servo_wobble_timer_));
        }
        servo_wobble_step_ = 0;
        servo_wobble_active_ = true;
        // Kick off the first step immediately.
        ServoWobbleStepAdvance();
    }

    static void ServoTaskTrampoline(void* arg) {
        static_cast<StackChanBoard*>(arg)->ServoTaskMain();
    }

    void ServoTaskMain() {
        constexpr TickType_t kInterFrameGap = pdMS_TO_TICKS(10);

        while (true) {
            vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));
            if (!servo_ok_) continue;

            AxisMotion yaw_local;
            AxisMotion pitch_local;
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            yaw_local = yaw_motion_;
            pitch_local = pitch_motion_;
            xSemaphoreGive(motion_mutex_);

            if (!yaw_local.moving && !pitch_local.moving) continue;

            uint32_t now_ms = static_cast<uint32_t>(esp_timer_get_time() / 1000);

            int new_yaw_current = yaw_local.current_deg;
            bool new_yaw_moving = yaw_local.moving;
            if (yaw_local.moving) {
                uint32_t elapsed = now_ms - yaw_local.move_start_ms;
                if (elapsed >= yaw_local.move_duration_ms) {
                    new_yaw_current = yaw_local.target_deg;
                    new_yaw_moving = false;
                } else {
                    int delta = yaw_local.target_deg - yaw_local.start_deg;
                    new_yaw_current = yaw_local.start_deg +
                        static_cast<int>(static_cast<int64_t>(delta) * elapsed / yaw_local.move_duration_ms);
                }
            }

            int new_pitch_current = pitch_local.current_deg;
            bool new_pitch_moving = pitch_local.moving;
            if (pitch_local.moving) {
                uint32_t elapsed = now_ms - pitch_local.move_start_ms;
                if (elapsed >= pitch_local.move_duration_ms) {
                    new_pitch_current = pitch_local.target_deg;
                    new_pitch_moving = false;
                } else {
                    int delta = pitch_local.target_deg - pitch_local.start_deg;
                    new_pitch_current = pitch_local.start_deg +
                        static_cast<int>(static_cast<int64_t>(delta) * elapsed / pitch_local.move_duration_ms);
                }
            }

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
            if (yaw_motion_.move_start_ms == yaw_local.move_start_ms) {
                yaw_motion_.current_deg = new_yaw_current;
            }
            if (!new_yaw_moving && yaw_motion_.target_deg == yaw_local.target_deg
                && yaw_motion_.move_start_ms == yaw_local.move_start_ms) {
                yaw_motion_.moving = false;
            }
            if (pitch_motion_.move_start_ms == pitch_local.move_start_ms) {
                pitch_motion_.current_deg = new_pitch_current;
            }
            if (!new_pitch_moving && pitch_motion_.target_deg == pitch_local.target_deg
                && pitch_motion_.move_start_ms == pitch_local.move_start_ms) {
                pitch_motion_.moving = false;
            }
            xSemaphoreGive(motion_mutex_);
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
    // Phase 3 may add `self.display.set_eyes` if needed.

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

        ESP_LOGI(TAG, "StackChan MCP tools registered");
    }

public:
    StackChanBoard() {
        InitializePowerSaveTimer();
        InitializeI2c();
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
