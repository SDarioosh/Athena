# odysseus-sound PDA firmware for the LilyGO T5 E-Paper Pro (docs/t5-pda-plan.md).
#
# A separate ESP-IDF project (plan §11 Q1 default). It compiles a few pure modules from
# ../recorder/main (crc32, isotime) without changing them, so recorder behaviour is untouched.
#
#   idf.py build
cmake_minimum_required(VERSION 3.16)

# Build only what main pulls in (no Wi-Fi in the image).
set(COMPONENTS main)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(pda)
