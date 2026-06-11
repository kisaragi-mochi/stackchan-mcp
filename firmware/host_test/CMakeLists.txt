cmake_minimum_required(VERSION 3.16)

project(stackchan_firmware_host_tests LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include(FetchContent)

FetchContent_Declare(
    googletest
    GIT_REPOSITORY https://github.com/google/googletest.git
    GIT_TAG v1.17.0
)
FetchContent_MakeAvailable(googletest)

enable_testing()

add_executable(mdns_candidate_extract_test
    test_mdns_candidate_extract.cc
    ../main/protocols/mdns_candidate_extract.cc
)

target_compile_definitions(mdns_candidate_extract_test
    PRIVATE STACKCHAN_MDNS_HOST_TEST
)

target_include_directories(mdns_candidate_extract_test
    PRIVATE
        stubs
        ../main/protocols
)

target_link_libraries(mdns_candidate_extract_test
    PRIVATE GTest::gtest_main
)

include(GoogleTest)
gtest_discover_tests(mdns_candidate_extract_test)
