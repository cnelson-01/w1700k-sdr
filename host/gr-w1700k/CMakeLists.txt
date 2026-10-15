# SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.16)
project(gr-w1700k VERSION 0.1.0 LANGUAGES CXX)

include(GNUInstallDirs)
include(CTest)
find_package(Gnuradio 3.10 REQUIRED COMPONENTS blocks)
find_package(pybind11 REQUIRED)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(gnuradio-w1700k SHARED lib/stream_source_impl.cpp)
target_compile_features(gnuradio-w1700k PUBLIC cxx_std_17)
target_include_directories(gnuradio-w1700k PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE ../stream/include)
target_link_libraries(gnuradio-w1700k PUBLIC gnuradio::gnuradio-runtime PRIVATE Volk::volk)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_libraries(gnuradio-w1700k PRIVATE rt)
endif()
target_compile_options(gnuradio-w1700k PRIVATE -Wall -Wextra -Wpedantic)
set_target_properties(gnuradio-w1700k PROPERTIES VERSION ${PROJECT_VERSION} SOVERSION 0
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/python/gnuradio/w1700k
    BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH_USE_LINK_PATH TRUE)

pybind11_add_module(w1700k_python python/bindings.cpp)
target_link_libraries(w1700k_python PRIVATE gnuradio-w1700k)
set_target_properties(w1700k_python PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/python/gnuradio/w1700k)
# The extension may be installed outside the dynamic linker's default search path.
if(APPLE)
    set(_origin "@loader_path")
else()
    set(_origin "$ORIGIN")
endif()
get_filename_component(_python_install "${GR_PYTHON_DIR}" ABSOLUTE BASE_DIR "${CMAKE_INSTALL_PREFIX}")
get_filename_component(_lib_install "${CMAKE_INSTALL_LIBDIR}" ABSOLUTE BASE_DIR "${CMAKE_INSTALL_PREFIX}")
file(RELATIVE_PATH _lib_relative "${_python_install}/gnuradio/w1700k" "${_lib_install}")
# Co-locate the build-tree library with the extension. Baking the final paths
# into both also avoids repeated install_name_tool rewrites on macOS reinstalls.
set_target_properties(w1700k_python PROPERTIES
    BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH_USE_LINK_PATH TRUE
    INSTALL_RPATH "${_origin};${_origin}/${_lib_relative}")

foreach(_file __init__.py source.py control.py)
    configure_file(python/w1700k/${_file} python/gnuradio/w1700k/${_file} COPYONLY)
    install(FILES python/w1700k/${_file} DESTINATION ${GR_PYTHON_DIR}/gnuradio/w1700k)
endforeach()
install(TARGETS gnuradio-w1700k EXPORT w1700kTargets LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(TARGETS w1700k_python LIBRARY DESTINATION ${GR_PYTHON_DIR}/gnuradio/w1700k)
install(DIRECTORY include/gnuradio DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(FILES grc/w1700k_source.block.yml DESTINATION ${GRC_BLOCKS_DIR})
install(FILES README.md examples/receive.py DESTINATION ${CMAKE_INSTALL_DATADIR}/doc/gr-w1700k)
include(CMakePackageConfigHelpers)
configure_package_config_file(cmake/gnuradio-w1700kConfig.cmake.in
    ${CMAKE_CURRENT_BINARY_DIR}/gnuradio-w1700kConfig.cmake
    INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/gnuradio-w1700k)
write_basic_package_version_file(${CMAKE_CURRENT_BINARY_DIR}/gnuradio-w1700kConfigVersion.cmake
    VERSION ${PROJECT_VERSION} COMPATIBILITY SameMajorVersion)
install(EXPORT w1700kTargets NAMESPACE gnuradio:: DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/gnuradio-w1700k)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/gnuradio-w1700kConfig.cmake
    ${CMAKE_CURRENT_BINARY_DIR}/gnuradio-w1700kConfigVersion.cmake
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/gnuradio-w1700k)

if(BUILD_TESTING)
    add_library(w1700k_test_ring SHARED tests/ring_fixture.cpp)
    target_include_directories(w1700k_test_ring PRIVATE ../stream/include)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
        target_link_libraries(w1700k_test_ring PRIVATE rt)
    endif()
    add_test(NAME w1700k_flowgraphs COMMAND ${PYTHON_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/tests/qa_source.py)
    set_tests_properties(w1700k_flowgraphs PROPERTIES TIMEOUT 90
        ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_BINARY_DIR}/python;PYTHONDONTWRITEBYTECODE=1;W1700K_TEST_RING=$<TARGET_FILE:w1700k_test_ring>")
endif()
