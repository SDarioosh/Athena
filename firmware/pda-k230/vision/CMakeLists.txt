# athena-vision: camera AI of the K230 PDA (docs/k230-pda-plan.md, Contract 2).
#
# Host (tests, replay):   cmake -S . -B build && cmake --build build && ctest --test-dir build
# With sanitizers:        cmake -S . -B build-asan -DVISION_SANITIZE=ON ...
# Device (Buildroot):     -DVISION_DEVICE=ON with Buildroot's toolchain file; see README.md.
cmake_minimum_required(VERSION 3.13)
project(athena_vision VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(VISION_DEVICE "Build the athena-vision program for the board (nncase, vvcam)" OFF)
if(VISION_DEVICE)
    set(vision_host_default OFF)
else()
    set(vision_host_default ON)
endif()
option(VISION_TESTS "Build the host unit tests" ${vision_host_default})
option(VISION_TOOLS "Build the replay and synth_track tools" ${vision_host_default})
option(VISION_SANITIZE "Build with -fsanitize=address,undefined" OFF)
option(VISION_WERROR "Treat warnings as errors" OFF)
option(VISION_K230_CPU_FLAGS "Add the Xuantie C908 flags Canaan's demos use (device)" ON)
set(VISION_QR "zxing" CACHE STRING "QR decoder on the device: zxing, opencv or none")
set_property(CACHE VISION_QR PROPERTY STRINGS zxing opencv none)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

set(VISION_WARNINGS -Wall -Wextra -Wshadow)
if(VISION_WERROR)
    list(APPEND VISION_WARNINGS -Werror)
endif()
if(VISION_SANITIZE)
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all)
    add_link_options(-fsanitize=address,undefined)
endif()

# ---- the pure core: no camera, no models, no OpenCV ----
add_library(athena_vision_core STATIC
    src/core/canaan_gesture.cpp
    src/core/config.cpp
    src/core/engine.cpp
    src/core/frame_check.cpp
    src/core/gesture_tracker.cpp
    src/core/hand_shape.cpp
    src/core/json.cpp
    src/core/mode.cpp
    src/core/orientation.cpp
    src/core/presence.cpp
    src/core/protocol.cpp
    src/core/track.cpp
    src/net/line_client.cpp
)
target_include_directories(athena_vision_core PUBLIC src/core src/net)
target_compile_options(athena_vision_core PRIVATE ${VISION_WARNINGS})
target_compile_definitions(athena_vision_core PUBLIC ATHENA_VISION_VERSION="${PROJECT_VERSION}")

if(VISION_TOOLS OR VISION_TESTS)
    add_library(athena_vision_synth STATIC tools/synth.cpp)
    target_link_libraries(athena_vision_synth PUBLIC athena_vision_core)
    target_include_directories(athena_vision_synth PUBLIC tools)
    target_compile_options(athena_vision_synth PRIVATE ${VISION_WARNINGS})
endif()

if(VISION_TOOLS)
    add_executable(athena-vision-replay tools/replay.cpp)
    target_link_libraries(athena-vision-replay PRIVATE athena_vision_core)
    target_compile_options(athena-vision-replay PRIVATE ${VISION_WARNINGS})
    add_executable(synth_track tools/synth_track.cpp)
    target_link_libraries(synth_track PRIVATE athena_vision_synth)
    target_compile_options(synth_track PRIVATE ${VISION_WARNINGS})
    install(TARGETS athena-vision-replay RUNTIME DESTINATION bin)
endif()

if(VISION_TESTS)
    enable_testing()
    set(VISION_TEST_NAMES
        test_hand_shape test_tracker test_presence test_json test_protocol
        test_mode test_config test_engine test_track test_line_client test_frame_check)
    foreach(t ${VISION_TEST_NAMES})
        add_executable(${t} tests/${t}.cpp)
        target_link_libraries(${t} PRIVATE athena_vision_synth)
        target_compile_options(${t} PRIVATE ${VISION_WARNINGS})
        add_test(NAME ${t} COMMAND ${t})
    endforeach()
    if(VISION_TOOLS)
        # synth_track -> replay end to end: the events of known tracks.
        add_test(NAME replay_end_to_end
            COMMAND ${CMAKE_COMMAND}
                -DSYNTH=$<TARGET_FILE:synth_track> -DREPLAY=$<TARGET_FILE:athena-vision-replay>
                -DWORK=${CMAKE_CURRENT_BINARY_DIR}/replay_e2e
                -P ${CMAKE_CURRENT_SOURCE_DIR}/tests/replay_e2e.cmake)
    endif()
endif()

# ---- the device program ----
if(VISION_DEVICE)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(V4L2DRM REQUIRED v4l2-drm)
    pkg_check_modules(DISPLAY REQUIRED display)
    add_executable(athena-vision
        src/device/main.cpp
        src/device/camera.cpp
        src/device/kmodel.cpp
        src/device/hand_models.cpp
        src/device/face_model.cpp
        src/device/qr_reader.cpp
    )
    target_link_libraries(athena-vision PRIVATE athena_vision_core)
    target_compile_options(athena-vision PRIVATE ${VISION_WARNINGS})
    if(VISION_K230_CPU_FLAGS)
        target_compile_options(athena-vision PRIVATE
            -mcpu=c908v -mabi=lp64d -mtune=c908 -mrvv-v0p10-compatible -mrvv-auto-vectorize)
    endif()
    # nncase's runtime tarball installs its headers to <sysroot>/usr/include/nncase and its
    # static libraries to <sysroot>/usr/lib; gsl-lite to <sysroot>/usr/include/gsl; libmmz
    # is <sysroot>/usr/include/mmz.h and usr/lib/libmmz.a; display.h needs libdrm's headers.
    target_include_directories(athena-vision SYSTEM PRIVATE
        ${V4L2DRM_INCLUDE_DIRS} ${DISPLAY_INCLUDE_DIRS} ${CMAKE_SYSROOT}/usr/include/libdrm)
    target_link_directories(athena-vision PRIVATE ${V4L2DRM_LIBRARY_DIRS} ${DISPLAY_LIBRARY_DIRS})
    target_link_libraries(athena-vision PRIVATE
        -Wl,--start-group Nncase.Runtime.Native nncase.rt_modules.k230 functional_k230
        v4l2-drm display drm mmz pthread -Wl,--end-group)
    if(VISION_QR STREQUAL "zxing")
        find_package(ZXing REQUIRED)
        target_link_libraries(athena-vision PRIVATE ZXing::ZXing)
        target_compile_definitions(athena-vision PRIVATE VISION_QR_ZXING=1)
    elseif(VISION_QR STREQUAL "opencv")
        find_package(OpenCV REQUIRED COMPONENTS core imgproc objdetect)
        target_include_directories(athena-vision SYSTEM PRIVATE ${OpenCV_INCLUDE_DIRS})
        target_link_libraries(athena-vision PRIVATE opencv_objdetect opencv_imgproc opencv_core)
        target_compile_definitions(athena-vision PRIVATE VISION_QR_OPENCV=1)
    endif()
    install(TARGETS athena-vision RUNTIME DESTINATION bin)
endif()

install(FILES vision.conf.example DESTINATION share/athena-vision)
