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
    DropdownChoice,
    ListOf,
    ListOfStrings,
    MonitoringState,
    TextAscii,
)
from cmk.gui.plugins.wato import (
    RulespecGroupCheckParametersApplications,
    RulespecGroupCheckParametersDiscovery,
    register_check_parameters,
    register_rule,
)

register_rule(
    RulespecGroupCheckParametersDiscovery,
    varname="discovery_systemd_units_services_rules",
    title=_("Systemd Service Discovery"),
    valuespec=Dictionary(
        elements=[
            ('descriptions', ListOfStrings(title=_("Descriptions"))),
            ('names', ListOfStrings(title=_("Service unit names"))),
            ('states',
             ListOf(
                 DropdownChoice(
                     choices=[
                         ("active", "active"),
                         ("inactive", "inactive"),
                         ("failed", "failed"),
                     ],),
                 title=_("States"),
             )),
        ],
        help=_('This rule can be used to configure the discovery of the Linux services check. '
               'You can configure specific Linux services to be monitored by the Linux check by '
               'selecting them by description, unit name, or current state during the discovery.'),
    ),
    match='all',
)

register_check_parameters(
    RulespecGroupCheckParametersApplications,
    "systemd_services",
    _("Systemd Services"),
    Dictionary(
        elements=[
            ("states",
             Dictionary(
                 elements=[
                     ("active",
                      MonitoringState(
                          title=_("Monitoring state if service is active"),
                          default_value=0,
                      )),
                 ],)),
            ("states_default",
             MonitoringState(
                 title=_("Monitoring state for any other service state"),
                 default_value=2,
             )),
            ("else",
             MonitoringState(
                 title=_("Monitoring state if a monitored service is not found at all."),
                 default_value=2,
             )),
        ],),
    TextAscii(title=_("Name of the service")),
    match_type="dict",
)