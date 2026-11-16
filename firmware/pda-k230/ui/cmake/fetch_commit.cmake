# Fetch one commit of a git repository, shallow (GitHub serves a full SHA with
# `git fetch --depth 1`; CMake's own GIT_SHALLOW needs a branch or tag).
#   cmake -DREPO=<url> -DCOMMIT=<full sha> -DDIR=<dir> -P fetch_commit.cmake
foreach(v REPO COMMIT DIR)
    if(NOT ${v})
        message(FATAL_ERROR "fetch_commit.cmake: ${v} not set")
    endif()
endforeach()
find_package(Git REQUIRED)

if(EXISTS "${DIR}/.git")
    execute_process(COMMAND ${GIT_EXECUTABLE} rev-parse HEAD WORKING_DIRECTORY "${DIR}"
                    OUTPUT_VARIABLE head OUTPUT_STRIP_TRAILING_WHITESPACE RESULT_VARIABLE rc)
    if(rc EQUAL 0 AND head STREQUAL COMMIT)
        return()
    endif()
endif()

file(MAKE_DIRECTORY "${DIR}")
macro(git)
    execute_process(COMMAND ${GIT_EXECUTABLE} ${ARGN} WORKING_DIRECTORY "${DIR}" RESULT_VARIABLE rc)
    if(NOT rc EQUAL 0)
        message(FATAL_ERROR "git ${ARGN} failed (${rc})")
    endif()
endmacro()
if(NOT EXISTS "${DIR}/.git")
    git(init -q)
    git(remote add origin "${REPO}")
endif()
git(fetch -q --depth 1 origin "${COMMIT}")
git(checkout -q --force FETCH_HEAD)
