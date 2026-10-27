add_library(athena_core STATIC
  src/core.cpp
)
target_include_directories(athena_core
  PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include"
  PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src")
target_compile_options(athena_core PRIVATE -Wall -Wextra)
target_link_libraries(athena_core PUBLIC Threads::Threads PkgConfig::CURL PkgConfig::OPUSENC)
set_target_properties(athena_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
