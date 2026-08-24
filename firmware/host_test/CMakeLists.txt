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

add_executable(listening_profile_test
    test_listening_profile.cc
    ../main/listening_profile.cc
)

add_executable(voice_interaction_test
    test_voice_interaction.cc
    ../main/voice_interaction.cc
)

add_executable(mcp_action_reconnect_policy_test
    test_mcp_action_reconnect_policy.cc
)

target_include_directories(mcp_action_reconnect_policy_test
    PRIVATE
        ../main/protocols
)

target_link_libraries(mcp_action_reconnect_policy_test
    PRIVATE GTest::gtest_main
)

target_include_directories(voice_interaction_test
    PRIVATE
        ../main
)

target_link_libraries(voice_interaction_test
    PRIVATE GTest::gtest_main
)

target_include_directories(listening_profile_test
    PRIVATE
        ../main
)

target_link_libraries(listening_profile_test
    PRIVATE GTest::gtest_main
)

include(GoogleTest)
gtest_discover_tests(mdns_candidate_extract_test)
gtest_discover_tests(listening_profile_test)
gtest_discover_tests(voice_interaction_test)
gtest_discover_tests(mcp_action_reconnect_policy_test)
