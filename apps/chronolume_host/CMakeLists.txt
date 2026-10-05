set(CHRONOLUME_ROOT "${CMAKE_SOURCE_DIR}")
if(APPLE)
    set(VITURE_ROOT "${CHRONOLUME_ROOT}/vendor/viture/macos")
    set(VITURE_LIB_DIR "${VITURE_ROOT}/aarch64")
    set(VITURE_LIB_NAME "libglasses.dylib")
elseif(WIN32)
    set(VITURE_ROOT "${CHRONOLUME_ROOT}/vendor/viture/windows")
    set(VITURE_LIB_DIR "${VITURE_ROOT}/x86_64")
    set(VITURE_LIB_NAME "glasses.dll")
else()
    set(VITURE_ROOT "${CHRONOLUME_ROOT}/vendor/viture/macos")
    set(VITURE_LIB_DIR "")
    set(VITURE_LIB_NAME "")
endif()

option(CHRONOLUME_WITH_VITURE "Compile VITURE backend (loaded at runtime)" ON)
if(NOT VITURE_LIB_NAME OR NOT EXISTS "${VITURE_LIB_DIR}/${VITURE_LIB_NAME}")
    set(CHRONOLUME_WITH_VITURE OFF)
    message(WARNING "VITURE library not found at ${VITURE_LIB_DIR}/${VITURE_LIB_NAME}; building mock-only")
endif()

include(FetchContent)
FetchContent_Declare(
    glfw
    GIT_REPOSITORY https://github.com/glfw/glfw.git
    GIT_TAG 3.4
    GIT_SHALLOW TRUE
)
set(GLFW_BUILD_DOCS OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(GLFW_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(glfw)

set(HOST_SOURCES
    src/main.cpp
    src/control/control_server.cpp
    src/device/mock_device.cpp
    src/display/frame_image.cpp
    src/display/stimulus.cpp
    src/logging/session_log.cpp
)
if(APPLE)
    list(APPEND HOST_SOURCES src/display/macos_colorspace.m)
endif()
if(CHRONOLUME_WITH_VITURE)
    list(APPEND HOST_SOURCES src/device/viture_device.cpp)
endif()

add_executable(chronolume_host ${HOST_SOURCES})

target_include_directories(chronolume_host PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${VITURE_ROOT}/include"
)

target_compile_definitions(chronolume_host PRIVATE
    CHRONOLUME_ROOT="${CHRONOLUME_ROOT}"
    GL_SILENCE_DEPRECATION
)
if(CHRONOLUME_WITH_VITURE)
    target_compile_definitions(chronolume_host PRIVATE
        CHRONOLUME_WITH_VITURE=1
        CHRONOLUME_VITURE_LIBDIR="${VITURE_LIB_DIR}"
    )
endif()

target_link_libraries(chronolume_host PRIVATE glfw)

if(APPLE)
    target_link_libraries(chronolume_host PRIVATE
        "-framework OpenGL"
        "-framework Cocoa"
        "-framework IOKit"
        "-framework CoreFoundation"
    )
    set_source_files_properties(src/display/macos_colorspace.m
        PROPERTIES COMPILE_FLAGS "-fobjc-arc")
elseif(WIN32)
    target_compile_definitions(chronolume_host PRIVATE
        NOMINMAX
        WIN32_LEAN_AND_MEAN
        _CRT_SECURE_NO_WARNINGS
    )
    target_link_libraries(chronolume_host PRIVATE opengl32 ws2_32 setupapi)
else()
    find_package(OpenGL REQUIRED)
    target_link_libraries(chronolume_host PRIVATE OpenGL::GL)
endif()
