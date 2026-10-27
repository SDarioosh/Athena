# Core host tests: no Zoom SDK; the upload tests talk to a fake server on loopback.
pkg_check_modules(OPUSFILE IMPORTED_TARGET opusfile)  # optional: decodes the Opus output

add_executable(core_unit_test core_unit_test.cpp)
target_include_directories(core_unit_test PRIVATE "${PROJECT_SOURCE_DIR}/core/src")
target_link_libraries(core_unit_test PRIVATE athena_core)
if(OPUSFILE_FOUND)
  target_compile_definitions(core_unit_test PRIVATE HAVE_OPUSFILE)
  target_link_libraries(core_unit_test PRIVATE PkgConfig::OPUSFILE)
endif()
add_test(NAME core_unit COMMAND core_unit_test)

add_executable(core_driver core_driver.cpp)
target_link_libraries(core_driver PRIVATE athena_core)

find_package(Python3 COMPONENTS Interpreter)
if(Python3_FOUND)
  add_test(NAME core_upload
           COMMAND Python3::Interpreter "${CMAKE_CURRENT_SOURCE_DIR}/test_core_upload.py"
                   --driver $<TARGET_FILE:core_driver>
                   --server "${CMAKE_CURRENT_SOURCE_DIR}/fake_ingest_server.py")
  set_tests_properties(core_upload PROPERTIES TIMEOUT 300)
else()
  message(WARNING "python3 not found: the core upload tests are skipped")
endif()
