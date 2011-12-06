#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2010             mk@mathias-kettner.de |
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

import grp, defaults, pprint, os, gettext

nagios_state_names = { -1: "NODATA", 0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN", 4: "DEPENDENT" }
nagios_short_state_names = { -1: "PEND", 0: "OK", 1: "WARN", 2: "CRIT", 3: "UNKN", 4: "DEP" }
nagios_short_host_state_names = { 0: "UP", 1: "DOWN", 2: "UNREACH" }

class MKGeneralException(Exception):
    def __init__(self, reason):
        self.reason = reason
    def __str__(self):
        return str(self.reason)

class MKAuthException(Exception):
    def __init__(self, reason):
        self.reason = reason
    def __str__(self):
        return str(self.reason)

class MKUnauthenticatedException(MKGeneralException):
    pass

class MKConfigError(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)

class MKUserError(Exception):
    def __init__(self, varname, msg):
        self.varname = varname
        self.message = msg
        Exception.__init__(self, msg)

class MKInternalError(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)

# Create directory owned by common group of Nagios and webserver,
# and make it writable for the group
def make_nagios_directory(path):
    if not os.path.exists(path):
        try:
            os.mkdir(path)
            gid = grp.getgrnam(defaults.www_group).gr_gid
            os.chown(path, -1, gid)
            os.chmod(path, 0770)
        except Exception, e:
            raise MKConfigError("Your web server cannot create the directory <tt>%s</tt>, "
                    "or cannot set the group to <tt>%s</tt> or cannot set the permissions to <tt>0770</tt>. "
                    "Please make sure that:<ul><li>the base directory is writable by the web server.</li>"
                    "<li>Both Nagios and the web server are in the group <tt>%s</tt>.</ul>Reason: %s" % (
                        path, defaults.www_group, defaults.www_group, e))

def create_user_file(path, mode):
    f = file(path, mode, 0)
    gid = grp.getgrnam(defaults.www_group).gr_gid
    # Tackle user problem: If the file is owned by nagios, the web
    # user can write it but cannot chown the group. In that case we
    # assume that the group is correct and ignore the error
    try:
        os.chown(path, -1, gid)
        os.chmod(path, 0660)
    except:
        pass
    return f

def write_settings_file(path, content):
    create_user_file(path, "w").write(pprint.pformat(content) + "\n")

def savefloat(f):
    try:
        return float(f)
    except:
        return 0.0


# Load all files below share/check_mk/web/plugins/WHAT into a
# specified context (global variables). Also honors the
# local-hierarchy for OMD
def load_web_plugins(forwhat, globalvars):
    plugins_path = defaults.web_dir + "/plugins/" + forwhat

    fns = os.listdir(plugins_path)
    fns.sort()
    for fn in fns:
        if fn.endswith(".py"):
            execfile(plugins_path + "/" + fn, globalvars)

    if defaults.omd_root:
        local_plugins_path = defaults.omd_root + "/local/share/check_mk/web/plugins/" + forwhat
        if local_plugins_path != plugins_path: # honor ./setup.sh in site
            if os.path.exists(local_plugins_path):
                fns = os.listdir(local_plugins_path)
                fns.sort()
                for fn in fns:
                    if fn.endswith(".py"):
                        execfile(local_plugins_path + "/" + fn, globalvars)

def get_languages():
    languages = []
    dirs = [ defaults.locale_dir ]
    if defaults.omd_root:
        dirs.append(defaults.omd_root + "/local/share/check_mk/locale")

    for lang_dir in dirs:
        try:
            languages += [ (val, val) for val in os.listdir(lang_dir) if not '.' in val ]
        except OSError:
            # Catch "OSError: [Errno 2] No such file or directory:" when directory not exists
            pass

    return languages

def pnp_cleanup(s):
    return s \
        .replace(' ', '_') \
        .replace(':', '_') \
        .replace('/', '_') \
        .replace('\\', '_')

def format_exception():
    import traceback, StringIO, sys
    txt = StringIO.StringIO()
    t, v, tb = sys.exc_info()
    traceback.print_exception(t, v, tb, None, txt)
    return txt.getvalue()


