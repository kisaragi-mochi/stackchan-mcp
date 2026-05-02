// avatar_images.cc — PLACEHOLDER (1×1 black pixel per symbol)
// Replace with real 160×120 RGB565 art before shipping to production.
//
// Fix for issue #9: was 2.73 MB (14 symbols × ~38400 bytes of 0x00 text).
// Now: 14 symbols × 2 bytes = 28 bytes of data, ~1 KB total file.
//
// Each lv_image_dsc_t is a 1×1 black RGB565 pixel.
// LVGL renders a single black pixel; device boots without crashing.

#include "avatar_images.h"

// ─── Phase 1: full-face expressions ──────────────────────────────────────────

static const uint8_t _avatar_idle_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_idle = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_idle_data,
};

static const uint8_t _avatar_happy_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_happy = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_happy_data,
};

static const uint8_t _avatar_thinking_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_thinking = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_thinking_data,
};

static const uint8_t _avatar_sad_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_sad = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_sad_data,
};

static const uint8_t _avatar_surprised_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_surprised = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_surprised_data,
};

static const uint8_t _avatar_embarrassed_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_embarrassed = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_embarrassed_data,
};

// ─── Phase 2: eye states ──────────────────────────────────────────────────────

static const uint8_t _avatar_eyes_open_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_eyes_open = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_eyes_open_data,
};

static const uint8_t _avatar_eyes_half_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_eyes_half = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_eyes_half_data,
};

static const uint8_t _avatar_eyes_closed_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_eyes_closed = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_eyes_closed_data,
};

// ─── Phase 2: mouth states ────────────────────────────────────────────────────

static const uint8_t _avatar_mouth_closed_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_mouth_closed = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_mouth_closed_data,
};

static const uint8_t _avatar_mouth_half_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_mouth_half = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_mouth_half_data,
};

static const uint8_t _avatar_mouth_open_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_mouth_open = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_mouth_open_data,
};

static const uint8_t _avatar_mouth_e_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_mouth_e = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_mouth_e_data,
};

static const uint8_t _avatar_mouth_u_data[] = {0x00, 0x00};
const lv_image_dsc_t avatar_mouth_u = {
    .header = {
        .cf          = LV_COLOR_FORMAT_RGB565,
        .flags       = 0,
        .w           = 1,
        .h           = 1,
        .stride      = 2,
    },
    .data_size = 2,
    .data      = _avatar_mouth_u_data,
};
