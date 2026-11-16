# synth_track -> athena-vision-replay: each synthetic track gives exactly the expected
# gesture events (and nothing else). Run by ctest (replay_end_to_end).
cmake_policy(SET CMP0007 NEW)
file(MAKE_DIRECTORY ${WORK})

# kind | fps | extra synth args (comma separated) | expected gestures (space separated, in order)
set(CASES
    "swipe_left|15||swipe_left"
    "swipe_right|8|--jitter,0.02|swipe_right"
    "swipe_up|30|--drop,0.15,--seed,3|swipe_up"
    "swipe_down|15|--rotate,0,--mirror,false|swipe_down"
    "return_left|15||swipe_left"
    "palm|15||palm"
    "fist|8||fist"
    "thumbs_up|30|--rotate,270|thumbs_up"
    "point|15||point"
    "drift|15||"
)

set(failed 0)
foreach(c ${CASES})
    string(REPLACE "|" ";" parts "${c}")
    list(LENGTH parts n)
    list(GET parts 0 kind)
    list(GET parts 1 fps)
    set(extra "")
    set(expect "")
    if(n GREATER 2)
        list(GET parts 2 extra)
        string(REPLACE "," ";" extra "${extra}")
    endif()
    if(n GREATER 3)
        list(GET parts 3 expect)
    endif()
    set(track ${WORK}/${kind}_${fps}.jsonl)
    execute_process(COMMAND ${SYNTH} ${kind} --fps ${fps} ${extra}
        OUTPUT_FILE ${track} RESULT_VARIABLE rc)
    if(NOT rc EQUAL 0)
        message(SEND_ERROR "synth_track ${kind} failed (${rc})")
        math(EXPR failed "${failed}+1")
        continue()
    endif()
    execute_process(COMMAND ${REPLAY} --raw --mode gestures ${track}
        OUTPUT_VARIABLE out ERROR_VARIABLE err RESULT_VARIABLE rc)
    if(NOT rc EQUAL 0)
        message(SEND_ERROR "replay ${kind} failed (${rc}): ${err}")
        math(EXPR failed "${failed}+1")
        continue()
    endif()
    string(REGEX MATCHALL "\"g\":\"[a-z_]+\"" found "${out}")
    set(got "")
    foreach(f ${found})
        string(REGEX REPLACE "\"g\":\"([a-z_]+)\"" "\\1" g "${f}")
        string(APPEND got "${g} ")
    endforeach()
    string(STRIP "${got}" got)
    # the cursor stream must end with `none`
    string(REGEX MATCHALL "\"shape\":\"[a-z_]+\"" shapes "${out}")
    list(LENGTH shapes nshapes)
    set(last_shape "")
    if(nshapes GREATER 0)
        list(GET shapes -1 last_shape)
    endif()
    if(NOT got STREQUAL "${expect}")
        message(SEND_ERROR "${kind} @${fps} fps: expected '${expect}', got '${got}'")
        math(EXPR failed "${failed}+1")
    elseif(NOT last_shape STREQUAL "\"shape\":\"none\"")
        message(SEND_ERROR "${kind} @${fps} fps: hand stream doesn't end with none")
        math(EXPR failed "${failed}+1")
    else()
        message(STATUS "ok ${kind} @${fps} fps: '${got}'")
    endif()
endforeach()
if(failed GREATER 0)
    message(FATAL_ERROR "${failed} replay case(s) failed")
endif()
