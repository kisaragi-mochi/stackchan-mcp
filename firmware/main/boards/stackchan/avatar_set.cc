#include "avatar_set.h"

#include <cstring>

#include <esp_heap_caps.h>
#include <esp_log.h>

#define TAG "AvatarSet"

AvatarSet::AvatarSet() = default;

AvatarSet::~AvatarSet() {
    Unload();
}

const lv_image_dsc_t* AvatarSet::GetFace(int face_index) const {
    if (!loaded_ || mode_ != Mode::kLayered) {
        return nullptr;
    }
    if (face_index < 0 || face_index >= kNumFaces) {
        return nullptr;
    }
    return &face_table_[face_index];
}

const lv_image_dsc_t* AvatarSet::GetEyes(int eyes_index) const {
    if (!loaded_ || mode_ != Mode::kLayered) {
        return nullptr;
    }
    if (eyes_index < 0 || eyes_index >= kNumEyes) {
        return nullptr;
    }
    return &eyes_table_[eyes_index];
}

const lv_image_dsc_t* AvatarSet::GetMouth(int mouth_index) const {
    if (!loaded_ || mode_ != Mode::kLayered) {
        return nullptr;
    }
    if (mouth_index < 0 || mouth_index >= kNumMouths) {
        return nullptr;
    }
    return &mouth_table_[mouth_index];
}

const lv_image_dsc_t* AvatarSet::GetMatrix(int face_index, int eyes_index, int mouth_index) const {
    if (!loaded_ || mode_ != Mode::kMatrix) {
        return nullptr;
    }
    if (face_index < 0 || face_index >= kNumFaces ||
        eyes_index < 0 || eyes_index >= kNumEyes ||
        mouth_index < 0 || mouth_index >= kNumMouths) {
        return nullptr;
    }
    const int idx = face_index * kNumEyes * kNumMouths
                  + eyes_index * kNumMouths
                  + mouth_index;
    return &matrix_table_[idx];
}

bool AvatarSet::AdoptOwnedBuffer(Mode mode, uint8_t* owned_buffer, size_t image_data_size) {
    if (owned_buffer == nullptr) {
        ESP_LOGW(TAG, "AdoptOwnedBuffer: owned_buffer is null");
        return false;
    }

    const size_t expected =
        (mode == Mode::kLayered) ? kLayeredPayloadBytes : kMatrixPayloadBytes;
    if (image_data_size != expected) {
        ESP_LOGW(TAG,
                 "AdoptOwnedBuffer: size mismatch (got %u, expected %u for mode=%d)",
                 static_cast<unsigned int>(image_data_size),
                 static_cast<unsigned int>(expected),
                 static_cast<int>(mode));
        // Ownership stays with caller — caller frees on false return.
        return false;
    }

    // Atomically swap: free previous buffer (= drop old descriptors first
    // for clarity) right before installing the new one. lv_image_dsc_t::data
    // pointers are repopulated below, so the LCD draws from the old buffer
    // up to this point and from the new one on the next LVGL invalidation.
    Unload();

    image_buffer_ = owned_buffer;
    image_buffer_size_ = image_data_size;
    mode_ = mode;

    if (mode == Mode::kLayered) {
        size_t offset = 0;
        for (int i = 0; i < kNumFaces; ++i) {
            InitImageHeader(&face_table_[i]);
            face_table_[i].data = image_buffer_ + offset;
            offset += kImageBytes;
        }
        for (int i = 0; i < kNumEyes; ++i) {
            InitImageHeader(&eyes_table_[i]);
            eyes_table_[i].data = image_buffer_ + offset;
            offset += kImageBytes;
        }
        for (int i = 0; i < kNumMouths; ++i) {
            InitImageHeader(&mouth_table_[i]);
            mouth_table_[i].data = image_buffer_ + offset;
            offset += kImageBytes;
        }
    } else {
        for (int i = 0; i < kMatrixSize; ++i) {
            InitImageHeader(&matrix_table_[i]);
            matrix_table_[i].data = image_buffer_ + static_cast<size_t>(i) * kImageBytes;
        }
    }

    loaded_ = true;
    ESP_LOGI(TAG, "Avatar set adopted: mode=%d, bytes=%u",
             static_cast<int>(mode),
             static_cast<unsigned int>(image_data_size));
    return true;
}

void AvatarSet::Unload() {
    if (image_buffer_ != nullptr) {
        heap_caps_free(image_buffer_);
        image_buffer_ = nullptr;
        image_buffer_size_ = 0;
    }

    // Clear all lv_image_dsc_t entries — `data` pointers no longer valid.
    std::memset(face_table_, 0, sizeof(face_table_));
    std::memset(eyes_table_, 0, sizeof(eyes_table_));
    std::memset(mouth_table_, 0, sizeof(mouth_table_));
    std::memset(matrix_table_, 0, sizeof(matrix_table_));

    loaded_ = false;
}

void AvatarSet::InitImageHeader(lv_image_dsc_t* dsc) {
    dsc->header.magic     = LV_IMAGE_HEADER_MAGIC;
    dsc->header.cf        = LV_COLOR_FORMAT_RGB565;
    dsc->header.flags     = 0;
    dsc->header.w         = kImageWidth;
    dsc->header.h         = kImageHeight;
    dsc->header.stride    = kImageWidth * 2;
    dsc->header.reserved_2 = 0;
    dsc->data_size = kImageBytes;
    // `data` is set by the caller (PSRAM offset).
}
