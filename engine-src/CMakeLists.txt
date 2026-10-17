# engine-src/CMakeLists.txt - the HyperCore process boundary, added to a Strata clone.
#
# Upstream Strata has no worker target. This file is copied into the clone before
# configure, and adds:
#   hypercore_api    - a shared library exposing the engine as a C API
#   hypercore-worker - a thin process that speaks the framed UNIX-socket protocol
#                      to the web server and drives strata --serve over stdin/stdout
#
# The point is the blast radius: an engine abort takes down the worker, not the
# web server, and engine threads never touch the Python GIL.
#
# If this fails to compile against a given Strata version, the build falls back to
# engine-only and the server runs in direct stdin/stdout mode.

cmake_minimum_required(VERSION 3.16)
project(hypercore_interface CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_library(hypercore_api SHARED
  interface/hypercore_bridge.cpp
  interface/engine_process.cpp
  interface/cli_gateway.cpp
)
target_include_directories(hypercore_api PUBLIC include)
target_compile_features(hypercore_api PRIVATE cxx_std_20)
set_target_properties(hypercore_api PROPERTIES OUTPUT_NAME "hypercore_api")

add_executable(hypercore-worker interface/worker_main.cpp)
target_include_directories(hypercore-worker PRIVATE include)
target_link_libraries(hypercore-worker PRIVATE hypercore_api)

if(CMAKE_C_COMPILER)
  add_library(glibc_compat STATIC interface/glibc_compat.c)
  target_link_libraries(hypercore-worker PRIVATE glibc_compat)
endif
