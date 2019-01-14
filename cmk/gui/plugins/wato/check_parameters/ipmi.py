#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# tails. You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

from cmk.gui.i18n import _
from cmk.gui.valuespec import (
    Dictionary,
    Float,
    ListOf,
    ListOfStrings,
    MonitoringState,
    TextAscii,
    Tuple,
)
from cmk.gui.plugins.wato import (
    RulespecGroupCheckParametersEnvironment,
    register_check_parameters,
)

register_check_parameters(
    RulespecGroupCheckParametersEnvironment,
    "ipmi",
    _("IPMI sensors"),
    Dictionary(
        elements=[
            ("sensor_states",
             ListOf(
                 Tuple(elements=[TextAscii(), MonitoringState()]),
                 title=_("Set states of IPMI sensor status texts"),
                 help=_("The pattern specified here must match exactly the beginning of "
                        "the sensor state (case sensitive)."),
                 orientation="horizontal",
             )),
            ("ignored_sensors",
             ListOfStrings(
                 title=_("Ignore the following IPMI sensors"),
                 help=_("Names of IPMI sensors that should be ignored during discovery "
                        "and when summarizing."
                        "The pattern specified here must match exactly the beginning of "
                        "the actual sensor name (case sensitive)."),
                 orientation="horizontal")),
            ("ignored_sensorstates",
             ListOfStrings(
                 title=_("Ignore the following IPMI sensor states"),
                 help=_("IPMI sensors with these states that should be ignored during discovery "
                        "and when summarizing."
                        "The pattern specified here must match exactly the beginning of "
                        "the actual sensor name (case sensitive)."),
                 orientation="horizontal",
                 default_value=["nr", "ns"])),
            ("numerical_sensor_levels",
             ListOf(
                 Tuple(
                     elements=[
                         TextAscii(
                             title=_("Sensor name (only summary)"),
                             help=_(
                                 "In summary mode you have to state the sensor name. "
                                 "In single mode the sensor name comes from service description.")),
                         Dictionary(elements=[
                             ("lower",
                              Tuple(title=_("Lower levels"), elements=[Float(), Float()])),
                             ("upper",
                              Tuple(title=_("Upper levels"), elements=[Float(), Float()])),
                         ]),
                     ],),
                 title=_("Set lower and upper levels for numerical sensors"))),
        ],
        ignored_keys=["ignored_sensors", "ignored_sensor_states"],
    ),
    TextAscii(title=_("The sensor name")),
    "dict",
)