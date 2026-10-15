// SPDX-License-Identifier: MIT
#include <gnuradio/w1700k/stream_source.h>
#include <pybind11/functional.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;
PYBIND11_MODULE(w1700k_python, m)
{
    py::module::import("gnuradio.gr");
    using gr::w1700k::stream_source;
    py::class_<stream_source, gr::sync_block, gr::block, gr::basic_block,
               std::shared_ptr<stream_source>>(m, "stream_source")
        .def(py::init(&stream_source::make), py::arg("name") = "/w1700k-stream", py::arg("writable") = false)
        .def("set_metadata", &stream_source::set_metadata)
        .def("set_radio_state", &stream_source::set_radio_state)
        .def("restart_run", &stream_source::restart_run)
        .def("stats", &stream_source::stats)
        .def("last_error", &stream_source::last_error)
        .def("_set_lifecycle", &stream_source::set_lifecycle);
}
