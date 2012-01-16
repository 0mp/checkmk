#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2012             mk@mathias-kettner.de |
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
# ails.  You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

from lib import *


def edit_dictionary(entries, value, focus=None, hover_help=True):
    new_value = value.copy()
    if html.check_transaction():
        messages = []
        for name, vs in entries: 
            try:
                v = vs.from_html_vars(name)
                vs.validate_value(v, name)
                new_value[name] = v
            except MKUserError, e:
                messages.append(vs.title() + ": " + e.message)
                html.add_user_error(e.varname, e.message)
        if messages:
            html.show_error("".join(["%s<br>\n" % m for m in messages]))
        else:
            return new_value

    html.begin_form("form")
    html.write("<table class=form>\n")
    first = True
    for name, vs in entries:

        html.write("<tr><td ")
        if vs.help() and hover_help:
            html.write('title="%s" ' % vs.help().replace('"', "&quot;"))
        html.write("class=legend>%s" % vs.title())
        if vs.help() and not hover_help:
            html.write("<br><i>%s</i>" % vs.help())
        html.write("</td><td class=content>")
        if name in value:
            v = value[name]
        else:
            v = vs.default_value()
        vs.render_input(name, v)
        if (not focus and first) or (name == focus):
            vs.set_focus(name)
            first = False 
    html.write("<tr><td class=buttons colspan=2>")
    html.button("save", _("Save"))
    html.write("</td></tr>\n")
    html.write("</table>\n")
    html.hidden_fields()
    html.end_form()


