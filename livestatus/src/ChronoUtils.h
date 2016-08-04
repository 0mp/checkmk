// +------------------------------------------------------------------+
// |             ____ _               _        __  __ _  __           |
// |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
// |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
// |           | |___| | | |  __/ (__|   <    | |  | | . \            |
// |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
// |                                                                  |
// | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
// +------------------------------------------------------------------+
//
// This file is part of Check_MK.
// The official homepage is at http://mathias-kettner.de/check_mk.
//
// check_mk is free software;  you can redistribute it and/or modify it
// under the  terms of the  GNU General Public License  as published by
// the Free Software Foundation in version 2.  check_mk is  distributed
// in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
// out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
// PARTICULAR PURPOSE. See the  GNU General Public License for more de-
// tails. You should have  received  a copy of the  GNU  General Public
// License along with GNU Make; see the file  COPYING.  If  not,  write
// to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
// Boston, MA 02110-1301 USA.

#ifndef ChronoUtils_h
#define ChronoUtils_h

#include "config.h"  // IWYU pragma: keep
#include <sys/time.h>
#include <chrono>
#include <cstdlib>
#include <ctime>
#include <string>

inline double elapsed_ms_since(std::chrono::system_clock::time_point then) {
    using namespace std::chrono;
    return duration_cast<duration<double, std::milli>>(system_clock::now() -
                                                       then)
        .count();
}

inline tm to_tm(std::chrono::system_clock::time_point tp) {
    time_t t = std::chrono::system_clock::to_time_t(tp);
    struct tm ret;
    localtime_r(&t, &ret);
    return ret;
}

template <typename Rep, typename Period>
inline timeval to_timeval(std::chrono::duration<Rep, Period> dur) {
    using namespace std::chrono;
    timeval tv;
    tv.tv_sec = static_cast<time_t>(duration_cast<seconds>(dur).count());
    tv.tv_usec = static_cast<suseconds_t>(
        duration_cast<microseconds>(dur % seconds(1)).count());
    return tv;
}

inline std::chrono::system_clock::time_point parse_time_t(
    const std::string& str) {
    return std::chrono::system_clock::from_time_t(atoi(str.c_str()));
}

#endif  // ChronoUtils_h
