# engine-src/CMakeLists.txt - the HyperCore process boundary, copied into a Strata clone.
#
# Upstream Strata has no worker target. This adds one: hypercore-worker is a small
# process that speaks the framed UNIX-socket protocol to the web server and drives
# "strata --serve" as its own child over stdin/stdout.
#
# The point is the blast radius. An engine abort takes down the worker, not the web
# server, and the engine's threads never touch the Python GIL.
#
# Nothing here needs the engine source - only the binary and its --serve protocol -
# so this builds against a stock Strata checkout.

cmake_minimum_required(VERSION 3.16)
project(hypercore_interface CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# The Build tab copies these binaries into engine/engine_<quant>/ and then deletes the
# build tree, so the loader has to find libhypercore_api.so next to the binary it was
# copied with. cmake's default RUNPATH is the build tree, which is gone by the time
# the engine runs, and a plain file copy has no install step to rewrite it - the worker
# dies at startup with "error while loading shared libraries: libhypercore_api.so".
# $ORIGIN is the directory of the binary itself, and BUILD_WITH_INSTALL_RPATH puts that
# rpath on the binary at link time so a copied engine is self-contained without patchelf.
set(CMAKE_INSTALL_RPATH "$ORIGIN")
set(CMAKE_BUILD_WITH_INSTALL_RPATH ON)

add_library(hypercore_api SHARED
  interface/strata_bridge.cpp
  interface/engine_process.cpp
)
target_include_directories(hypercore_api PUBLIC include interface)
target_compile_features(hypercore_api PRIVATE cxx_std_20)
set_target_properties(hypercore_api PROPERTIES OUTPUT_NAME "hypercore_api")

add_executable(hypercore-worker interface/worker_main.cpp)
target_include_directories(hypercore-worker PRIVATE include interface)
target_link_libraries(hypercore-worker PRIVATE hypercore_api)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/interface/glibc_compat.c")
  add_library(glibc_compat STATIC interface/glibc_compat.c)
  target_link_libraries(hypercore-worker PRIVATE glibc_compat)
endif()
