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

import config
import defaults
import livestatus

# The global livestatus object. This is initialized automatically upon first access
# to the accessor function live()
_live = None

# site_status keeps a dictionary for each site with the following keys:
# "state"              --> "online", "disabled", "down", "unreach", "dead" or "waiting"
# "exception"          --> An error exception in case of down, unreach, dead or waiting
# "status_host_state"  --> host state of status host (0, 1, 2 or None)
# "livestatus_version" --> Version of sites livestatus if "online"
# "program_version"    --> Version of Nagios if "online"
_site_status = None


# Accessor and initializer to the global livestatus connection object
def live():
    if _live == None:
        connect()
    return _live


# Accessor for the status of a single site
def state(site_id, deflt=None):
    if _live == None:
        connect()
    return _site_status.get(site_id, deflt)


# Returns dictionary of all known site states
def states():
    if _live == None:
        connect()
    return _site_status


# Build up a connection to livestatus to either a single site or multiple sites.
def connect():
    init_site_status()

    # If there is only one site (non-multisite), then user cannot enable/disable.
    if config.is_multisite():
        connect_multiple_sites()
    else:
        connect_single_site()

    set_livestatus_auth()


def connect_multiple_sites():
    global _live
    enabled_sites, disabled_sites = get_enabled_and_disabled_sites()

    set_initial_site_states(enabled_sites, disabled_sites)

    _live = livestatus.MultiSiteConnection(enabled_sites, disabled_sites)

    # Fetch status of sites by querying the version of Nagios and livestatus
    # This may be cached by a proxy for up to the next configuration reload.
    _live.set_prepend_site(True)
    for site_id, v1, v2, ps, num_hosts, num_services in _live.query(
          "GET status\n"
          "Cache: reload\n"
          "Columns: livestatus_version program_version program_start num_hosts num_services"):
        update_site_status(site_id, {
            "state"              : "online",
            "livestatus_version" : v1,
            "program_version"    : v2,
            "program_start"      : ps,
            "num_hosts"          : num_hosts,
            "num_services"       : num_services,
            "core"               : v2.startswith("Check_MK") and "cmc" or "nagios",
        })
    _live.set_prepend_site(False)

    # Get exceptions in case of dead sites
    for site_id, deadinfo in _live.dead_sites().items():
        shs = deadinfo.get("status_host_state")
        update_site_status(site_id, {
            "exception"         : deadinfo["exception"],
            "status_host_state" : shs,
            "state"             : status_host_state_name(shs),
        })


def get_enabled_and_disabled_sites():
    enabled_sites, disabled_sites = {}, {}

    for site_id, site in config.allsites().items():
        siteconf = config.user_siteconf.get(site_id, {})
        # Convert livestatus-proxy links into UNIX socket
        s = site["socket"]
        if type(s) == tuple and s[0] == "proxy":
            site["socket"] = "unix:" + defaults.livestatus_unix_socket + "proxy/" + site_id
            site["cache"] = s[1].get("cache", True)
        else:
            site["cache"] = False

        if siteconf.get("disabled", False):
            disabled_sites[site_id] = site
        else:
            enabled_sites[site_id] = site

    return enabled_sites, disabled_sites


def connect_single_site():
    global _live
    _live = livestatus.SingleSiteConnection("unix:" + defaults.livestatus_unix_socket)
    _live.set_timeout(3) # default timeout is 3 seconds

    set_initial_site_states({"": config.site("")}, {})

    v1, v2, ps = _live.query_row("GET status\nColumns: livestatus_version program_version program_start")
    update_local_site_status({
        "state"              : "online",
        "livestatus_version" : v1,
        "program_version"    : v2,
        "program_start"      : ps,
    })


# If Multisite is retricted to data the user is a contact for, we need to set an
# AuthUser: header for livestatus.
def set_livestatus_auth():
    user_id = livestatus_auth_user()
    if user_id != None:
        _live.set_auth_user('read',   user_id)
        _live.set_auth_user('action', user_id)

    # May the user see all objects in BI aggregations or only some?
    if not config.may("bi.see_all"):
        _live.set_auth_user('bi', user_id)

    # May the user see all Event Console events or only some?
    if not config.may("mkeventd.seeall"):
        _live.set_auth_user('ec', user_id)

    # Default auth domain is read. Please set to None to switch off authorization
    _live.set_auth_domain('read')


# Returns either None when no auth user shal be set or the name of the user
# to be used as livestatus auth user
def livestatus_auth_user():
    if not config.may("general.see_all"):
        return config.user_id

    force_authuser = html.var("force_authuser")
    if force_authuser == "1":
        return config.user_id
    elif force_authuser == "0":
        return None
    elif force_authuser:
        return force_authuser # set a different user

    # TODO: Remove this with 1.5.0/1.6.0
    if html.output_format != 'html' and config.user.get("force_authuser_webservice"):
        return config.user_id

    if config.user.get("force_authuser"):
        return config.user_id

    return None


def disconnect():
    global _live, _site_status
    _live = None
    _site_status = None


def status_host_state_name(shs):
    if shs == None:
        return "dead"
    else:
        return { 1:"down", 2:"unreach", 3:"waiting", }.get(shs, "unknown")


def init_site_status():
    global _site_status
    _site_status = {}


def set_initial_site_states(enabled_sites, disabled_sites):
    for site_id, site in enabled_sites.items():
        set_site_status(site_id, {
            "state" : "dead",
            "site" : site
        })

    for site_id, site in disabled_sites.items():
        set_site_status(site_id, {
            "state" : "disabled",
            "site" : site
        })


def set_local_site_status(status):
    set_site_status("", status)


def set_site_status(site_id, status):
    _site_status[site_id] = status


def update_local_site_status(status):
    update_site_status("", status)


def update_site_status(site_id, status):
    _site_status[site_id].update(status)
