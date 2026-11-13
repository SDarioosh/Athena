# Cross-compiling for the K230 (64-bit RISC-V Linux) with a GNU toolchain, e.g. Ubuntu's
# gcc-riscv64-linux-gnu or the Buildroot one (set CROSS to its prefix):
#   cmake -S firmware/pda-k230/ui -B build-k230 -DCMAKE_TOOLCHAIN_FILE=firmware/pda-k230/ui/cmake/riscv64-linux-gnu.cmake
# Add -DPDA_DEVICE=ON with a sysroot that has libdrm (Buildroot's staging dir as
# CMAKE_SYSROOT, its pkg-config on PATH).
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR riscv64)
if(NOT CROSS)
    set(CROSS riscv64-linux-gnu-)
endif()
set(CMAKE_C_COMPILER ${CROSS}gcc)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
