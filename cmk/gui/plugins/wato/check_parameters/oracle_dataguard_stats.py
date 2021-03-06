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
    Age,
    Dictionary,
    TextAscii,
    Tuple,
)

from cmk.gui.plugins.wato import (
    CheckParameterRulespecWithItem,
    rulespec_registry,
    RulespecGroupCheckParametersApplications,
)


@rulespec_registry.register
class RulespecCheckgroupParametersOracleDataguardStats(CheckParameterRulespecWithItem):
    @property
    def group(self):
        return RulespecGroupCheckParametersApplications

    @property
    def check_group_name(self):
        return "oracle_dataguard_stats"

    @property
    def title(self):
        return _("Oracle Data-Guard Stats")

    @property
    def match_type(self):
        return "dict"

    @property
    def parameter_valuespec(self):
        return Dictionary(
            help=
            _("The Data-Guard statistics are available in Oracle Enterprise Edition with enabled Data-Guard. "
              "The <tt>init.ora</tt> parameter <tt>dg_broker_start</tt> must be <tt>TRUE</tt> for this check. "
              "The apply and transport lag can be configured with this rule."),
            elements=[
                ("apply_lag",
                 Tuple(
                     title=_("Apply Lag Maximum Time"),
                     help=_("The maximum limit for the apply lag in <tt>v$dataguard_stats</tt>."),
                     elements=[Age(title=_("Warning at"),),
                               Age(title=_("Critical at"),)],
                 )),
                ("apply_lag_min",
                 Tuple(
                     title=_("Apply Lag Minimum Time"),
                     help=_(
                         "The minimum limit for the apply lag in <tt>v$dataguard_stats</tt>. "
                         "This is only useful if also <i>Apply Lag Maximum Time</i> has been configured."
                     ),
                     elements=[Age(title=_("Warning at"),),
                               Age(title=_("Critical at"),)],
                 )),
                ("transport_lag",
                 Tuple(
                     title=_("Transport Lag"),
                     help=_("The limit for the transport lag in <tt>v$dataguard_stats</tt>"),
                     elements=[Age(title=_("Warning at"),),
                               Age(title=_("Critical at"),)],
                 )),
            ],
        )

    @property
    def item_spec(self):
        return TextAscii(title=_("Database SID"), size=12, allow_empty=False)
