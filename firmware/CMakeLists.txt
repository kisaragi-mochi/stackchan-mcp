# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# Add this line to disable the specific warning
add_compile_options(-Wno-missing-field-initializers)

if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.local")
    if(DEFINED SDKCONFIG_DEFAULTS)
        list(APPEND SDKCONFIG_DEFAULTS "sdkconfig.defaults.local")
    elseif(DEFINED ENV{SDKCONFIG_DEFAULTS} AND NOT "$ENV{SDKCONFIG_DEFAULTS}" STREQUAL "")
        set(SDKCONFIG_DEFAULTS "$ENV{SDKCONFIG_DEFAULTS};sdkconfig.defaults.local")
    else()
        set(SDKCONFIG_DEFAULTS "sdkconfig.defaults;sdkconfig.defaults.local")
    endif()
endif()

# Phase 1 PoC (#230): expose microlink's nested sub-components (kept under
# firmware/third-party/ rather than firmware/components/ to avoid an ESP-IDF
# component-name collision with the upstream wrapper top-level CMakeLists)
# so they're discoverable when CONFIG_STACKCHAN_TAILNET=y. Must precede
# project.cmake include because EXTRA_COMPONENT_DIRS is consumed during
# project() expansion. The EXCLUDE_COMPONENTS gate below (after project())
# filters them out of the default build, so OFF-side binaries see zero impact.
list(APPEND EXTRA_COMPONENT_DIRS "third-party/microlink/components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# "Trim" the build. Include the minimal set of components, main, and anything it depends on.
idf_build_set_property(MINIMAL_BUILD ON)

set(PROJECT_VER "2.2.6")
project(xiaozhi)

# Opt-in microlink (Tailscale ts2021) component (#230).
# Placed after project() so CONFIG_STACKCHAN_TAILNET reflects the sdkconfig
# values that project() loads (an `if(NOT CONFIG_*)` evaluated before project()
# would always succeed because the variable is undefined at that point, which
# would silently force the component to be excluded even when the user opts in).
# When CONFIG_STACKCHAN_TAILNET is not set, exclude the on-disk component
# pin from the build so it contributes zero bytes to the binary.
if(NOT CONFIG_STACKCHAN_TAILNET)
    idf_build_set_property(EXCLUDE_COMPONENTS "microlink;wireguard_lwip" APPEND)
endif()
