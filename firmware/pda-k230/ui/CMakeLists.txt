# athena-pda: the K230 PDA's UI (docs/k230-pda-plan.md). C11, LVGL 9.
#
#   cmake -S firmware/pda-k230/ui -B build-k230-ui && cmake --build build-k230-ui -j
#   ctest --test-dir build-k230-ui
#   build-k230-ui/pda_screenshots      # PNGs of every screen in build-k230-ui/screens/
#
# Options: -DPDA_DEVICE=ON (DRM + evdev; needs libdrm), -DPDA_SDL=ON (an SDL2 window; on by
# default when SDL2 is found). LVGL is fetched at the commit LilyGO's Linux image uses;
# -DFETCHCONTENT_SOURCE_DIR_LVGL=<checkout> builds offline from a local copy of it.
cmake_minimum_required(VERSION 3.16)
project(athena_pda C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()
# getrandom, fsync, clock_gettime, strdup... under -std=c11
add_compile_definitions(_DEFAULT_SOURCE _GNU_SOURCE _POSIX_C_SOURCE=200809L)

option(PDA_DEVICE "the K230: DRM/KMS display and evdev touch (needs libdrm)" OFF)
option(PDA_WERROR "warnings are errors in our code" ON)
option(PDA_TESTS "build the host tests and the screenshot tool" ON)
find_package(PkgConfig)
if(PKG_CONFIG_FOUND)
    pkg_check_modules(SDL2 IMPORTED_TARGET sdl2)
endif()
if(SDL2_FOUND AND NOT PDA_DEVICE AND NOT CMAKE_CROSSCOMPILING)
    set(PDA_SDL_DEFAULT ON)
else()
    set(PDA_SDL_DEFAULT OFF)
endif()
option(PDA_SDL "an SDL2 window on a PC" ${PDA_SDL_DEFAULT})

set(UI ${CMAKE_CURRENT_SOURCE_DIR})
set(T5 ${UI}/../../pda/main)
set(REC ${UI}/../../recorder)

# ---- LVGL (never vendored: fetched at the pinned commit) ----
include(FetchContent)
set(LVGL_REPO https://github.com/lvgl/lvgl.git)
# LVGL 9.5-dev, the commit LilyGO's T-Display-K230 Linux image builds
set(LVGL_COMMIT 59dc7e436ae97a25e32656739ea6a943f9f11b6a)
FetchContent_Declare(lvgl
    DOWNLOAD_COMMAND ${CMAKE_COMMAND} -DREPO=${LVGL_REPO} -DCOMMIT=${LVGL_COMMIT} -DDIR=<SOURCE_DIR>
                     -P ${UI}/cmake/fetch_commit.cmake
    SOURCE_SUBDIR no-cmake # our own build of it below, not LVGL's CMakeLists
)
FetchContent_MakeAvailable(lvgl)
set(LVGL_DIR ${lvgl_SOURCE_DIR})

file(GLOB_RECURSE LVGL_SOURCES CONFIGURE_DEPENDS ${LVGL_DIR}/src/*.c)
add_library(lvgl STATIC ${LVGL_SOURCES})
target_include_directories(lvgl SYSTEM PUBLIC ${LVGL_DIR} ${UI})
target_compile_definitions(lvgl PUBLIC LV_CONF_INCLUDE_SIMPLE)
target_compile_options(lvgl PRIVATE -w) # third-party code: its warnings aren't ours
if(PDA_DEVICE)
    pkg_check_modules(DRM REQUIRED IMPORTED_TARGET libdrm)
    target_compile_definitions(lvgl PUBLIC PDA_DEVICE=1)
    target_link_libraries(lvgl PUBLIC PkgConfig::DRM)
endif()
if(PDA_SDL)
    target_compile_definitions(lvgl PUBLIC PDA_SDL=1)
    target_link_libraries(lvgl PUBLIC PkgConfig::SDL2)
endif()
target_link_libraries(lvgl PUBLIC m)

# ---- our code ----
set(PDA_WARN -Wall -Wextra)
if(PDA_WERROR)
    list(APPEND PDA_WARN -Werror)
endif()

# the T5's pure modules, compiled from where they are (unchanged), and the core here
add_library(pda_core STATIC
    ${T5}/json.c ${T5}/feed.c ${T5}/pda_time.c ${T5}/today_plan.c ${T5}/outbox.c
    ${T5}/pda_actions.c ${T5}/feed_store.c ${T5}/pda_proto.c ${T5}/ui_nav.c ${T5}/gesture.c
    ${REC}/main/crc32.c ${REC}/main/isotime.c
    src/core/app_state.c src/core/glance_frame.c src/core/glance_server.c src/core/local_msg.c
    src/core/vision_msg.c src/core/vision_server.c src/core/handsfree.c src/core/today_text.c
    src/core/power.c src/core/board.c
)
target_include_directories(pda_core PUBLIC src/core ${T5} ${REC}/main)
target_compile_options(pda_core PRIVATE ${PDA_WARN})

# the fonts, embedded
include(cmake/embed_file.cmake)
set(FONT_C ${CMAKE_CURRENT_BINARY_DIR}/fonts_embedded)
file(MAKE_DIRECTORY ${FONT_C})
embed_file(font_inter_regular ${UI}/fonts/Inter-Regular.ttf ${FONT_C}/inter_regular.c)
embed_file(font_inter_semibold ${UI}/fonts/Inter-SemiBold.ttf ${FONT_C}/inter_semibold.c)
embed_file(font_inter_bold ${UI}/fonts/Inter-Bold.ttf ${FONT_C}/inter_bold.c)
embed_file(font_lucide ${UI}/fonts/lucide.ttf ${FONT_C}/lucide.c)

find_package(ZLIB)
add_library(pda_ui STATIC
    src/ui/theme.c src/ui/widgets.c src/ui/ui.c src/ui/chrome.c src/ui/handsfree_ui.c
    src/ui/ui_screens.c src/ui/screen_today.c src/ui/screen_tasks.c src/ui/screen_stub.c
    src/ui/sheet_event.c
    src/platform/platform.c src/platform/platform_headless.c src/platform/png_write.c
    ${FONT_C}/inter_regular.c ${FONT_C}/inter_semibold.c ${FONT_C}/inter_bold.c ${FONT_C}/lucide.c
)
target_include_directories(pda_ui PUBLIC src/ui src/platform)
target_link_libraries(pda_ui PUBLIC pda_core lvgl)
target_compile_options(pda_ui PRIVATE ${PDA_WARN})
set_source_files_properties(${FONT_C}/inter_regular.c ${FONT_C}/inter_semibold.c ${FONT_C}/inter_bold.c
    ${FONT_C}/lucide.c PROPERTIES COMPILE_OPTIONS "-w")
if(ZLIB_FOUND)
    target_compile_definitions(pda_ui PRIVATE PDA_HAVE_ZLIB=1)
    target_link_libraries(pda_ui PUBLIC ZLIB::ZLIB)
endif()

add_executable(athena-pda src/main.c)
target_link_libraries(athena-pda pda_ui)
target_compile_options(athena-pda PRIVATE ${PDA_WARN})
install(TARGETS athena-pda RUNTIME DESTINATION bin)

# ---- tests (no display) and the screenshot tool ----
if(PDA_TESTS)
    enable_testing()
    set(FIX ${UI}/../../pda/test/fixtures)
    foreach(t test_app_state test_glance test_vision test_today_text test_board)
        add_executable(${t} test/${t}.c)
        target_include_directories(${t} PRIVATE ${REC}/test)
        target_link_libraries(${t} pda_core)
        target_compile_options(${t} PRIVATE ${PDA_WARN})
        add_test(NAME ${t} COMMAND ${t})
        set_tests_properties(${t} PROPERTIES ENVIRONMENT "FIXTURES=${FIX}")
    endforeach()

    add_executable(pda_screenshots test/pda_screenshots.c)
    target_include_directories(pda_screenshots PRIVATE ${REC}/test)
    target_link_libraries(pda_screenshots pda_ui)
    target_compile_options(pda_screenshots PRIVATE ${PDA_WARN})
    add_test(NAME pda_screenshots COMMAND pda_screenshots)
    set_tests_properties(pda_screenshots PROPERTIES ENVIRONMENT
        "FIXTURES=${FIX};OUT_DIR=${CMAKE_CURRENT_BINARY_DIR}/screens")
endif()

# ---- Brief, Calendar, the new-event flow and the on-screen keyboard ----
target_sources(pda_core PRIVATE src/core/agenda.c src/core/event_form.c src/core/brief.c)
target_sources(pda_ui PRIVATE src/ui/controls.c src/ui/ui_keyboard.c)
if(PDA_TESTS)
    foreach(t test_agenda test_event_form test_brief)
        add_executable(${t} test/${t}.c)
        target_include_directories(${t} PRIVATE ${REC}/test)
        target_link_libraries(${t} pda_core)
        target_compile_options(${t} PRIVATE ${PDA_WARN})
        add_test(NAME ${t} COMMAND ${t})
        set_tests_properties(${t} PROPERTIES ENVIRONMENT "FIXTURES=${FIX}")
    endforeach()
endif()
