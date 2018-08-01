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

import os
import socket
import sys
import time
import inspect
import signal

from cmk.regex import regex
import cmk.tty as tty
import cmk.paths
from cmk.exceptions import MKGeneralException

import cmk.store as store
import cmk_base.crash_reporting
import cmk_base.config as config
import cmk_base.console as console
import cmk_base.piggyback as piggyback
import cmk_base.ip_lookup as ip_lookup
import cmk_base.check_api_utils as check_api_utils
import cmk_base.item_state as item_state
import cmk_base.checking as checking
import cmk_base.data_sources as data_sources
import cmk_base.check_table as check_table
import cmk_base.core
from cmk_base.exceptions import MKAgentError, MKParseFunctionError, MKTimeout
import cmk_base.cleanup
import cmk_base.check_utils
import cmk_base.decorator
import cmk_base.snmp_scan as snmp_scan

try:
    import cmk_base.cee.keepalive as keepalive
except ImportError:
    keepalive = None

# Run the discovery queued by check_discovery() - if any
_marked_host_discovery_timeout = 120

#   .--cmk -I--------------------------------------------------------------.
#   |                                  _           ___                     |
#   |                    ___ _ __ ___ | | __      |_ _|                    |
#   |                   / __| '_ ` _ \| |/ /  _____| |                     |
#   |                  | (__| | | | | |   <  |_____| |                     |
#   |                   \___|_| |_| |_|_|\_\      |___|                    |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Functions for command line options -I and -II                       |
#   '----------------------------------------------------------------------'

# Function implementing cmk -I and cmk -II. This is directly
# being called from the main option parsing code. The list of
# hostnames is already prepared by the main code. If it is
# empty then we use all hosts and switch to using cache files.
def do_discovery(hostnames, check_plugin_names, only_new):
    use_caches = data_sources.abstract.DataSource.get_may_use_cache_file()
    if not hostnames:
        console.verbose("Discovering services on all hosts\n")
        hostnames = config.all_active_realhosts()
        use_caches = True
    else:
        console.verbose("Discovering services on: %s\n" % ", ".join(hostnames))

    # For clusters add their nodes to the list. Clusters itself
    # cannot be discovered but the user is allowed to specify
    # them and we do discovery on the nodes instead.
    nodes = []
    cluster_hosts = []
    for h in hostnames:
        nodes = config.nodes_of(h)
        if nodes:
            cluster_hosts.append(h)
            hostnames += nodes

    # Then remove clusters and make list unique
    hostnames = list(set([ h for h in hostnames if not config.is_cluster(h) ]))
    hostnames.sort()

    # Now loop through all hosts
    for hostname in hostnames:
        console.section_begin(hostname)

        try:
            if cmk.debug.enabled():
                on_error = "raise"
            else:
                on_error = "warn"

            ipaddress = ip_lookup.lookup_ip_address(hostname)

            # Usually we disable SNMP scan if cmk -I is used without a list of
            # explicity hosts. But for host that have never been service-discovered
            # yet (do not have autochecks), we enable SNMP scan.
            do_snmp_scan = not use_caches or not _has_autochecks(hostname)

            sources = _get_sources_for_discovery(hostname, ipaddress, check_plugin_names, do_snmp_scan, on_error)
            multi_host_sections = _get_host_sections_for_discovery(sources, use_caches=use_caches)

            _do_discovery_for(hostname, ipaddress, sources, multi_host_sections, check_plugin_names, only_new, on_error)

        except Exception, e:
            if cmk.debug.enabled():
                raise
            console.section_error("%s" % e)
        finally:
            cmk_base.cleanup.cleanup_globals()

    # Check whether or not the cluster host autocheck files are still
    # existant. Remove them. The autochecks are only stored in the nodes
    # autochecks files these days.
    for hostname in cluster_hosts:
        _remove_autochecks_file(hostname)


def _do_discovery_for(hostname, ipaddress, sources, multi_host_sections, check_plugin_names, only_new, on_error):
    if not check_plugin_names:
        # In 'multi_host_sections = _get_host_sections_for_discovery(..)'
        # we've already discovered the right check plugin names.
        # _discover_services(..) would discover check plugin names again.
        # In order to avoid a second discovery (SNMP data source would do
        # another SNMP scan) we enforce this selection to be used.
        check_plugin_names = multi_host_sections.get_check_plugin_names()
        sources.enforce_check_plugin_names(check_plugin_names)

    console.step("Executing inventory plugins")
    new_items = _discover_services(hostname, ipaddress, sources, multi_host_sections, on_error=on_error)

    if not check_plugin_names and not only_new:
        old_items = [] # do not even read old file
    else:
        old_items = parse_autochecks_file(hostname)

    # There are three ways of how to merge existing and new discovered checks:
    # 1. -II without --checks=
    #        check_plugin_names is empty, only_new is False
    #    --> complete drop old services, only use new ones
    # 2. -II with --checks=
    #    --> drop old services of that types
    #        check_plugin_names is not empty, only_new is False
    # 3. -I
    #    --> just add new services
    #        only_new is True

    # Parse old items into a dict (ct, item) -> paramstring
    result = {}
    for check_plugin_name, item, paramstring in old_items:
        # Take over old items if -I is selected or if -II
        # is selected with --checks= and the check type is not
        # one of the listed ones
        if only_new or (check_plugin_names and check_plugin_name not in check_plugin_names):
            result[(check_plugin_name, item)] = paramstring

    stats, num_services = {}, 0
    for check_plugin_name, item, paramstring in new_items:
        if (check_plugin_name, item) not in result:
            result[(check_plugin_name, item)] = paramstring
            stats.setdefault(check_plugin_name, 0)
            stats[check_plugin_name] += 1
            num_services += 1

    final_items = []
    for (check_plugin_name, item), paramstring in result.items():
        final_items.append((check_plugin_name, item, paramstring))
    final_items.sort()
    _save_autochecks_file(hostname, final_items)

    found_check_plugin_names = stats.keys()
    found_check_plugin_names.sort()

    if found_check_plugin_names:
        for check_plugin_name in found_check_plugin_names:
            console.verbose("%s%3d%s %s\n" % (tty.green + tty.bold, stats[check_plugin_name], tty.normal, check_plugin_name))
        console.section_success("Found %d services" % num_services)
    else:
        console.section_success("Found nothing%s" % (only_new and " new" or ""))


# determine changed services on host.
# param mode: can be one of "new", "remove", "fixall", "refresh"
# param do_snmp_scan: if True, a snmp host will be scanned, otherwise uses only the check types
#                     previously discovereda
# param servic_filter: if a filter is set, it controls whether items are touched by the discovery.
#                       if it returns False for a new item it will not be added, if it returns
#                       False for a vanished item, that item is kept
def discover_on_host(mode, hostname, do_snmp_scan, use_caches, on_error="ignore", service_filter=None):
    counts = {
        "added"   : 0,
        "removed" : 0,
        "kept"    : 0
    }

    if hostname not in config.all_active_realhosts():
        return [0, 0, 0, 0], ""

    if service_filter is None:
        service_filter = lambda hostname, check_plugin_name, item: True

    err = None

    try:
        # in "refresh" mode we first need to remove all previously discovered
        # checks of the host, so that _get_host_services() does show us the
        # new discovered check parameters.
        if mode == "refresh":
            counts["removed"] += remove_autochecks_of(hostname) # this is cluster-aware!

        if config.is_cluster(hostname):
            ipaddress = None
        else:
            ipaddress = ip_lookup.lookup_ip_address(hostname)

        sources = _get_sources_for_discovery(hostname, ipaddress, check_plugin_names=None,
                                             do_snmp_scan=do_snmp_scan, on_error=on_error)

        multi_host_sections = _get_host_sections_for_discovery(sources, use_caches=use_caches)

        # Compute current state of new and existing checks
        services = _get_host_services(hostname, ipaddress, sources, multi_host_sections, on_error=on_error)

        # Create new list of checks
        new_items = {}
        for (check_plugin_name, item), (check_source, paramstring) in services.items():
            if check_source in ("custom", "legacy", "active", "manual"):
                continue # this is not an autocheck or ignored and currently not checked
                # Note discovered checks that are shadowed by manual checks will vanish
                # that way.

            if check_source == "new":
                if mode in ("new", "fixall", "refresh") and service_filter(hostname, check_plugin_name, item):
                    counts["added"] += 1
                    new_items[(check_plugin_name, item)] = paramstring

            elif check_source in ("old", "ignored"):
                # keep currently existing valid services in any case
                new_items[(check_plugin_name, item)] = paramstring
                counts["kept"]  += 1

            elif check_source == "vanished":
                # keep item, if we are currently only looking for new services
                # otherwise fix it: remove ignored and non-longer existing services
                if mode not in ("fixall", "remove") or not service_filter(hostname, check_plugin_name, item):
                    new_items[(check_plugin_name, item)] = paramstring
                    counts["kept"] += 1
                else:
                    counts["removed"] += 1

            # Silently keep clustered services
            elif check_source.startswith("clustered_"):
                new_items[(check_plugin_name, item)] = paramstring

            else:
                raise MKGeneralException("Unknown check source '%s'" % check_source)
        set_autochecks_of(hostname, new_items)

    except MKTimeout:
        raise # let general timeout through

    except Exception, e:
        if cmk.debug.enabled():
            raise
        err = str(e)
    return [counts["added"], counts["removed"], counts["kept"], counts["added"] + counts["kept"]], err


#.
#   .--Discovery Check-----------------------------------------------------.
#   |           ____  _                   _               _                |
#   |          |  _ \(_)___  ___      ___| |__   ___  ___| | __            |
#   |          | | | | / __|/ __|    / __| '_ \ / _ \/ __| |/ /            |
#   |          | |_| | \__ \ (__ _  | (__| | | |  __/ (__|   <             |
#   |          |____/|_|___/\___(_)  \___|_| |_|\___|\___|_|\_\            |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Active check for checking undiscovered services.                    |
#   '----------------------------------------------------------------------'

@cmk_base.decorator.handle_check_mk_check_result("discovery", "Check_MK Discovery")
def check_discovery(hostname, ipaddress):
    params = discovery_check_parameters(hostname) or \
             default_discovery_check_parameters()

    status, infotexts, long_infotexts, perfdata = 0, [], [], []

    # In case of keepalive discovery we always have an ipaddress. When called as non keepalive
    # ipaddress is always None
    if ipaddress is None and not config.is_cluster(hostname):
        ipaddress = ip_lookup.lookup_ip_address(hostname)

    sources = _get_sources_for_discovery(hostname, ipaddress, check_plugin_names=None,
                                         do_snmp_scan=params["inventory_check_do_scan"],
                                         on_error="raise")

    multi_host_sections = _get_host_sections_for_discovery(sources,
                              use_caches=data_sources.abstract.DataSource.get_may_use_cache_file())

    services = _get_host_services(hostname, ipaddress, sources, multi_host_sections, on_error="raise")

    need_rediscovery = False

    params_rediscovery = params.get("inventory_rediscovery", {})

    if params_rediscovery.get("service_whitelist", []) or\
            params_rediscovery.get("service_blacklist", []):
        # whitelist. if none is specified, this matches everything
        whitelist = regex("|".join(["(%s)" % pat for pat in params_rediscovery.get("service_whitelist", [".*"])]))
        # blacklist. if none is specified, this matches nothing
        blacklist = regex("|".join(["(%s)" % pat for pat in params_rediscovery.get("service_blacklist", ["(?!x)x"])]))

        item_filters = lambda hostname, check_plugin_name, item:\
                _discovery_filter_by_lists(hostname, check_plugin_name, item, whitelist, blacklist)
    else:
        item_filters = None

    for check_state, title, params_key, default_state in [
           ( "new",      "unmonitored", "severity_unmonitored", config.inventory_check_severity ),
           ( "vanished", "vanished",    "severity_vanished",   0 ),
        ]:

        affected_check_plugin_names = {}
        count = 0
        unfiltered = False

        for (check_plugin_name, item), (check_source, _unused_paramstring) in services.items():
            if check_source == check_state:
                count += 1
                affected_check_plugin_names.setdefault(check_plugin_name, 0)
                affected_check_plugin_names[check_plugin_name] += 1

                if not unfiltered and\
                        (item_filters is None or item_filters(hostname, check_plugin_name, item)):
                    unfiltered = True

                long_infotexts.append("%s: %s: %s" % (title, check_plugin_name,
                    config.service_description(hostname, check_plugin_name, item)))

        if affected_check_plugin_names:
            info = ", ".join([ "%s:%d" % e for e in affected_check_plugin_names.items() ])
            st = params.get(params_key, default_state)
            status = cmk_base.utils.worst_service_state(status, st)
            infotexts.append("%d %s services (%s)%s" % (count, title, info, check_api_utils.state_markers[st]))

            if params.get("inventory_rediscovery", False):
                mode = params["inventory_rediscovery"]["mode"]
                if unfiltered and\
                        ((check_state == "new"      and mode in ( 0, 2, 3 )) or
                         (check_state == "vanished" and mode in ( 1, 2, 3 ))):
                    need_rediscovery = True
        else:
            infotexts.append("no %s services found" % title)

    for (check_plugin_name, item), (check_source, _unused_paramstring) in services.items():
        if check_source == "ignored":
            long_infotexts.append("ignored: %s: %s" % (check_plugin_name,
                    config.service_description(hostname, check_plugin_name, item)))

    _set_rediscovery_flag(hostname, need_rediscovery)
    if need_rediscovery:
        infotexts.append("rediscovery scheduled")

    # Add data source information to check results
    for source in sources.get_data_sources():
        source_state, source_output, source_perfdata = source.get_summary_result()
        # Do not output informational (state = 0) things. These information are shown by the "Check_MK" service
        if source_state != 0:
            status = max(status, source_state)
            infotexts.append("[%s] %s" % (source.id(), source_output))

    return status, infotexts, long_infotexts, perfdata


# Compute the parameters for the discovery check for a host. Note:
# if the discovery check is disabled for that host, default parameters
# will be returned.
def discovery_check_parameters(hostname):
    entries = config.host_extra_conf(hostname, config.periodic_discovery)
    if entries:
        return entries[0]
    # Support legacy global configurations
    elif config.inventory_check_interval:
        return default_discovery_check_parameters()
    else:
        return None


def default_discovery_check_parameters():
    return {
        "check_interval"          : config.inventory_check_interval,
        "severity_unmonitored"    : config.inventory_check_severity,
        "severity_vanished"       : 0,
        "inventory_check_do_scan" : config.inventory_check_do_scan,
    }


def _set_rediscovery_flag(hostname, need_rediscovery):
    def touch(filename):
        if not os.path.exists(filename):
            f = open(filename, "w")
            f.close()

    autodiscovery_dir = cmk.paths.var_dir + '/autodiscovery'
    discovery_filename = os.path.join(autodiscovery_dir, hostname)
    if need_rediscovery:
        if not os.path.exists(autodiscovery_dir):
            os.makedirs(autodiscovery_dir)
        touch(discovery_filename)
    else:
        if os.path.exists(discovery_filename):
            try:
                os.remove(discovery_filename)
            except OSError:
                pass



class DiscoveryTimeout(Exception):
    pass


def _handle_discovery_timeout():
    raise DiscoveryTimeout()


def _set_discovery_timeout():
    signal.signal(signal.SIGALRM, _handle_discovery_timeout)
    # Add an additional 10 seconds as grace period
    signal.alarm(_marked_host_discovery_timeout + 10)


def _clear_discovery_timeout():
    signal.alarm(0)


def _get_autodiscovery_dir():
    return cmk.paths.var_dir + '/autodiscovery'


def discover_marked_hosts(core):
    console.verbose("Doing discovery for all marked hosts:\n")
    autodiscovery_dir = _get_autodiscovery_dir()

    if not os.path.exists(autodiscovery_dir):
        # there is obviously nothing to do
        console.verbose("  Nothing to do. %s is missing.\n" % autodiscovery_dir)
        return

    now_ts = time.time()
    end_time_ts = now_ts + _marked_host_discovery_timeout  # don't run for more than 2 minutes
    oldest_queued = _queue_age()
    all_hosts = config.all_configured_hosts()
    hosts = os.listdir(autodiscovery_dir)
    if not hosts:
        console.verbose("  Nothing to do. No hosts marked by discovery check.\n")

    activation_required = False
    try:
        _set_discovery_timeout()
        for hostname in hosts:
            if _discover_marked_host(hostname, all_hosts, now_ts, oldest_queued):
                activation_required = True

            if time.time() > end_time_ts:
                console.warning("  Timeout of %d seconds reached. Lets do the remaining hosts next time." % _marked_host_discovery_timeout)
                break
    except DiscoveryTimeout:
        pass
    finally:
        _clear_discovery_timeout()


    if activation_required:
        console.verbose("\nRestarting monitoring core with updated configuration...\n")
        if config.monitoring_core == "cmc":
            cmk_base.core.do_reload(core)
        else:
            cmk_base.core.do_restart(core)


def _discover_marked_host(hostname, all_hosts, now_ts, oldest_queued):
    services_changed = False

    mode_table = {
        0: "new",
        1: "remove",
        2: "fixall",
        3: "refresh"
    }

    console.verbose("%s%s%s:\n" % (tty.bold, hostname, tty.normal))
    host_flag_path = os.path.join(_get_autodiscovery_dir(), hostname)
    if hostname not in all_hosts:
        try:
            os.remove(host_flag_path)
        except OSError:
            pass
        console.verbose("  Skipped. Host does not exist in configuration. Removing mark.\n")
        return


    params = discovery_check_parameters(hostname) or default_discovery_check_parameters()
    params_rediscovery = params.get("inventory_rediscovery", {})
    if "service_blacklist" in params_rediscovery or "service_whitelist" in params_rediscovery:
        # whitelist. if none is specified, this matches everything
        whitelist = regex("|".join(["(%s)" % pat for pat in params_rediscovery.get("service_whitelist", [".*"])]))
        # blacklist. if none is specified, this matches nothing
        blacklist = regex("|".join(["(%s)" % pat for pat in params_rediscovery.get("service_blacklist", ["(?!x)x"])]))
        item_filters = lambda hostname, check_plugin_name, item:\
            _discovery_filter_by_lists(hostname, check_plugin_name, item, whitelist, blacklist)
    else:
        item_filters = None

    why_not = _may_rediscover(params, now_ts, oldest_queued)
    if not why_not:
        redisc_params = params["inventory_rediscovery"]
        console.verbose("  Doing discovery with mode '%s'...\n" % mode_table[redisc_params["mode"]])
        result, error = discover_on_host(mode_table[redisc_params["mode"]], hostname,
                                         do_snmp_scan=params["inventory_check_do_scan"],
                                         use_caches=True,
                                         service_filter=item_filters)
        if error is not None:
            if error:
                console.verbose("failed: %s\n" % error)
            else:
                # for offline hosts the error message is empty. This is to remain
                # compatible with the automation code
                console.verbose("  failed: host is offline\n")
        else:
            new_services, removed_services, kept_services, total_services = result
            if new_services == 0 and removed_services == 0 and kept_services == total_services:
                console.verbose("  nothing changed.\n")
            else:
                console.verbose("  %d new, %d removed, %d kept, %d total services.\n" % (tuple(result)))
                if redisc_params["activation"]:
                    services_changed = True

                # Now ensure that the discovery service is updated right after the changes
                schedule_discovery_check(hostname)

        # delete the file even in error case, otherwise we might be causing the same error
        # every time the cron job runs
        try:
            os.remove(host_flag_path)
        except OSError:
            pass
    else:
        console.verbose("  skipped: %s\n" % why_not)

    return services_changed


def _queue_age():
    autodiscovery_dir = _get_autodiscovery_dir()
    oldest = time.time()
    for filename in os.listdir(autodiscovery_dir):
        oldest = min(oldest, os.path.getmtime(autodiscovery_dir + "/" + filename))
    return oldest


def _may_rediscover(params, now_ts, oldest_queued):
    if "inventory_rediscovery" not in params:
        return "automatic discovery disabled for this host"

    now = time.gmtime(now_ts)
    for start_hours_mins, end_hours_mins in params["inventory_rediscovery"]["excluded_time"]:
        start_time = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday,
            start_hours_mins[0], start_hours_mins[1], 0,
            now.tm_wday, now.tm_yday, now.tm_isdst))

        end_time = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday,
            end_hours_mins[0], end_hours_mins[1], 0,
            now.tm_wday, now.tm_yday, now.tm_isdst))

        if start_time <= now <= end_time:
            return "we are currently in a disallowed time of day"

    if now_ts - oldest_queued < params["inventory_rediscovery"]["group_time"]:
        return "last activation is too recent"

    return None



def _discovery_filter_by_lists(hostname, check_plugin_name, item, whitelist, blacklist):
    description = config.service_description(hostname, check_plugin_name, item)
    return whitelist.match(description) is not None and\
        blacklist.match(description) is None

#.
#   .--Helpers-------------------------------------------------------------.
#   |                  _   _      _                                        |
#   |                 | | | | ___| |_ __   ___ _ __ ___                    |
#   |                 | |_| |/ _ \ | '_ \ / _ \ '__/ __|                   |
#   |                 |  _  |  __/ | |_) |  __/ |  \__ \                   |
#   |                 |_| |_|\___|_| .__/ \___|_|  |___/                   |
#   |                              |_|                                     |
#   +----------------------------------------------------------------------+
#   |  Various helper functions                                            |
#   '----------------------------------------------------------------------'


# TODO: Move to livestatus module!
def schedule_discovery_check(hostname):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(cmk.paths.livestatus_unix_socket)
        now = int(time.time())
        if 'cmk-inventory' in config.use_new_descriptions_for:
            command = "SCHEDULE_FORCED_SVC_CHECK;%s;Check_MK Discovery;%d" % (hostname, now)
        else:
            # TODO: Remove this old name handling one day
            command = "SCHEDULE_FORCED_SVC_CHECK;%s;Check_MK inventory;%d" % (hostname, now)

        # Ignore missing check and avoid warning in cmc.log
        if config.monitoring_core == "cmc":
            command += ";TRY"

        s.send("COMMAND [%d] %s\n" % (now, command))
    except Exception:
        if cmk.debug.enabled():
            raise


#.
#   .--Discovery-----------------------------------------------------------.
#   |              ____  _                                                 |
#   |             |  _ \(_)___  ___ _____   _____ _ __ _   _               |
#   |             | | | | / __|/ __/ _ \ \ / / _ \ '__| | | |              |
#   |             | |_| | \__ \ (_| (_) \ V /  __/ |  | |_| |              |
#   |             |____/|_|___/\___\___/ \_/ \___|_|   \__, |              |
#   |                                                  |___/               |
#   +----------------------------------------------------------------------+
#   |  Core code of actual service discovery                               |
#   '----------------------------------------------------------------------'

# Create a table of autodiscovered services of a host. Do not save
# this table anywhere. Do not read any previously discovered
# services. The table has the following columns:
# 1. Check type
# 2. Item
# 3. Parameter string (not evaluated)
#
# This function does not handle:
# - clusters
# - disabled services
#
# This function *does* handle:
# - disabled check typess
#
# on_error is one of:
# "ignore" -> silently ignore any exception
# "warn"   -> output a warning on stderr
# "raise"  -> let the exception come through
def _discover_services(hostname, ipaddress, sources, multi_host_sections, on_error):
    # Make hostname available as global variable in discovery functions
    # (used e.g. by ps-discovery)
    check_api_utils.set_hostname(hostname)

    discovered_services = []
    try:
        for check_plugin_name in sources.get_check_plugin_names():
            try:
                for item, paramstring in _execute_discovery(multi_host_sections, hostname, ipaddress, check_plugin_name, on_error):
                    discovered_services.append((check_plugin_name, item, paramstring))
            except (KeyboardInterrupt, MKTimeout):
                raise
            except Exception, e:
                if on_error == "raise":
                    raise
                elif on_error == "warn":
                    console.error("Discovery of '%s' failed: %s\n" % (check_plugin_name, e))

        return discovered_services

    except KeyboardInterrupt:
        raise MKGeneralException("Interrupted by Ctrl-C.")


def _get_sources_for_discovery(hostname, ipaddress, check_plugin_names, do_snmp_scan, on_error):
    sources = data_sources.DataSources(hostname, ipaddress)

    for source in sources.get_data_sources():
        if isinstance(source, data_sources.SNMPDataSource):
            source.set_on_error(on_error)
            source.set_do_snmp_scan(do_snmp_scan)
            source.set_use_snmpwalk_cache(False)
            source.set_ignore_check_interval(True)
            source.set_check_plugin_name_filter(snmp_scan.gather_snmp_check_plugin_names)

    # When check types are specified via command line, enforce them and disable auto detection
    if check_plugin_names:
        sources.enforce_check_plugin_names(check_plugin_names)

    return sources


def _get_host_sections_for_discovery(sources, use_caches):
    max_cachefile_age = config.inventory_max_cachefile_age if use_caches else 0
    return sources.get_host_sections(max_cachefile_age)


def _execute_discovery(multi_host_sections, hostname, ipaddress, check_plugin_name, on_error):
    # Skip this check type if is ignored for that host
    if config.service_ignored(hostname, check_plugin_name, None):
        return []

    try:
        discovery_function = config.check_info[check_plugin_name]["inventory_function"]
        if discovery_function == None:
            discovery_function = check_api_utils.no_discovery_possible
    except KeyError:
        raise MKGeneralException("No such check type '%s'" % check_plugin_name)

    # Now do the actual discovery
    try:
        # TODO: There is duplicate code with checking.execute_check(). Find a common place!
        try:
            section_content = multi_host_sections.get_section_content(hostname, ipaddress,
                                                        check_plugin_name, for_discovery=True)
        except MKParseFunctionError, e:
            if cmk.debug.enabled() or on_error == "raise":
                x = e.exc_info()
                if x[0] == item_state.MKCounterWrapped:
                    return []
                else:
                    # re-raise the original exception to not destory the trace. This may raise a MKCounterWrapped
                    # exception which need to lead to a skipped check instead of a crash
                    raise x[0], x[1], x[2]

            elif on_error == "warn":
                section_name = cmk_base.check_utils.section_name_of(check_plugin_name)
                console.warning("Exception while parsing agent section '%s': %s\n" % (section_name, e))

            return []

        if section_content is None: # No data for this check type
            return []

        # In case of SNMP checks but missing agent response, skip this check.
        # Special checks which still need to be called even with empty data
        # may declare this.
        if not section_content and cmk_base.check_utils.is_snmp_check(check_plugin_name) \
           and not config.check_info[check_plugin_name]["handle_empty_info"]:
            return []

        # Check number of arguments of discovery function. Note: This
        # check for the legacy API will be removed after 1.2.6.
        if len(inspect.getargspec(discovery_function).args) == 2:
            discovered_items = discovery_function(check_plugin_name, section_content) # discovery is a list of pairs (item, current_value)
        else:
            # New preferred style since 1.1.11i3: only one argument: section_content
            discovered_items = discovery_function(section_content)

        # tolerate function not explicitely returning []
        if discovered_items == None:
            discovered_items = []

        # New yield based api style
        elif type(discovered_items) != list:
            discovered_items = list(discovered_items)

        result = []
        for entry in discovered_items:
            if not isinstance(entry, tuple):
                console.error("%s: Check %s returned invalid discovery data (entry not a tuple): %r\n" %
                                                                     (hostname, check_plugin_name, repr(entry)))
                continue

            if len(entry) == 2: # comment is now obsolete
                item, paramstring = entry
            elif len(entry) == 3: # allow old school
                item, __, paramstring = entry
            else: # we really don't want longer tuples (or 1-tuples).
                console.error("%s: Check %s returned invalid discovery data (not 2 or 3 elements): %r\n" %
                                                                (hostname, check_plugin_name, repr(entry)))
                continue

            # Check_MK 1.2.7i3 defines items to be unicode strings. Convert non unicode
            # strings here seamless. TODO remove this conversion one day and replace it
            # with a validation that item needs to be of type unicode
            if type(item) == str:
                item = config.decode_incoming_string(item)

            description = config.service_description(hostname, check_plugin_name, item)
            # make sanity check
            if len(description) == 0:
                console.error("%s: Check %s returned empty service description - ignoring it.\n" %
                                                (hostname, check_plugin_name))
                continue

            result.append((item, paramstring))

    except Exception, e:
        if on_error == "warn":
            console.warning("  Exception in discovery function of check type '%s': %s" % (check_plugin_name, e))
        elif on_error == "raise":
            raise
        return []

    return result


# Creates a table of all services that a host has or could have according
# to service discovery. The result is a dictionary of the form
# (check_plugin_name, item) -> (check_source, paramstring)
# check_source is the reason/state/source of the service:
#    "new"           : Check is discovered but currently not yet monitored
#    "old"           : Check is discovered and already monitored (most common)
#    "vanished"      : Check had been discovered previously, but item has vanished
#    "legacy"        : Check is defined via legacy_checks
#    "active"        : Check is defined via active_checks
#    "custom"        : Check is defined via custom_checks
#    "manual"        : Check is a manual Check_MK check without service discovery
#    "ignored"       : discovered or static, but disabled via ignored_services
#    "clustered_new" : New service found on a node that belongs to a cluster
#    "clustered_old" : Old service found on a node that belongs to a cluster
# This function is cluster-aware
def _get_host_services(hostname, ipaddress, sources, multi_host_sections, on_error):
    if config.is_cluster(hostname):
        return _get_cluster_services(hostname, ipaddress, sources, multi_host_sections, on_error)
    else:
        return _get_node_services(hostname, ipaddress, sources, multi_host_sections, on_error)


# Do the actual work for a non-cluster host or node
def _get_node_services(hostname, ipaddress, sources, multi_host_sections, on_error):
    services = _get_discovered_services(hostname, ipaddress, sources, multi_host_sections, on_error)

    # Identify clustered services
    for (check_plugin_name, item), (check_source, paramstring) in services.items():
        try:
            descr = config.service_description(hostname, check_plugin_name, item)
        except Exception, e:
            if on_error == "raise":
                raise
            elif on_error == "warn":
                console.error("Invalid service description: %s\n" % e)
            else:
                continue # ignore

        if hostname != config.host_of_clustered_service(hostname, descr):
            if check_source == "vanished":
                del services[(check_plugin_name, item)] # do not show vanished clustered services here
            else:
                services[(check_plugin_name, item)] = ("clustered_" + check_source, paramstring)

    _merge_manual_services(services, hostname, on_error)
    return services


# Part of _get_node_services that deals with discovered services
def _get_discovered_services(hostname, ipaddress, sources, multi_host_sections, on_error):
    # Create a dict from check_plugin_name/item to check_source/paramstring
    services = {}

    # In 'multi_host_sections = _get_host_sections_for_discovery(..)'
    # we've already discovered the right check plugin names.
    # _discover_services(..) would discover check plugin names again.
    # In order to avoid a second discovery (SNMP data source would do
    # another SNMP scan) we enforce this selection to be used.
    check_plugin_names = multi_host_sections.get_check_plugin_names()
    sources.enforce_check_plugin_names(check_plugin_names)

    # Handle discovered services -> "new"
    new_items = _discover_services(hostname, ipaddress, sources, multi_host_sections, on_error)
    for check_plugin_name, item, paramstring in new_items:
        services.setdefault((check_plugin_name, item), ("new", paramstring))

    # Match with existing items -> "old" and "vanished"
    old_items = parse_autochecks_file(hostname)
    for check_plugin_name, item, paramstring in old_items:
        if (check_plugin_name, item) not in services:
            services[(check_plugin_name, item)] = ("vanished", paramstring)
        else:
            services[(check_plugin_name, item)] = ("old", paramstring)

    return services


# To a list of discovered services add/replace manual and active
# checks and handle ignoration
def _merge_manual_services(services, hostname, on_error):
    # Find manual checks. These can override discovered checks -> "manual"
    manual_items = check_table.get_check_table(hostname, skip_autochecks=True)
    for (check_plugin_name, item), (params, descr, _unused_deps) in manual_items.items():
        services[(check_plugin_name, item)] = ('manual', repr(params) )

    # Add legacy checks -> "legacy"
    legchecks = config.host_extra_conf(hostname, config.legacy_checks)
    for _unused_cmd, descr, _unused_perf in legchecks:
        services[('legacy', descr)] = ('legacy', 'None')

    # Add custom checks -> "custom"
    custchecks = config.host_extra_conf(hostname, config.custom_checks)
    for entry in custchecks:
        services[('custom', entry['service_description'])] = ('custom', 'None')

    # Similar for 'active_checks', but here we have parameters
    for acttype, rules in config.active_checks.items():
        entries = config.host_extra_conf(hostname, rules)
        for params in entries:
            descr = config.active_check_service_description(hostname, acttype, params)
            services[(acttype, descr)] = ('active', repr(params))

    # Handle disabled services -> "ignored"
    for (check_plugin_name, item), (check_source, paramstring) in services.items():
        if check_source in [ "legacy", "active", "custom" ]:
            # These are ignored later in get_check_preview
            # TODO: This needs to be cleaned up. The problem here is that service_description() can not
            # calculate the description of active checks and the active checks need to be put into
            # "[source]_ignored" instead of ignored.
            continue

        try:
            descr = config.service_description(hostname, check_plugin_name, item)
        except Exception, e:
            if on_error == "raise":
                raise
            elif on_error == "warn":
                console.error("Invalid service description: %s\n" % e)
            else:
                continue # ignore

        if config.service_ignored(hostname, check_plugin_name, descr):
            services[(check_plugin_name, item)] = ("ignored", paramstring)

    return services

# Do the work for a cluster
def _get_cluster_services(hostname, ipaddress, sources, multi_host_sections, on_error):
    nodes = config.nodes_of(hostname)

    # Get setting from cluster SNMP data source
    do_snmp_scan = False
    for source in sources.get_data_sources():
        if isinstance(source, data_sources.SNMPDataSource):
            do_snmp_scan = source.get_do_snmp_scan()

    # Get services of the nodes. We are only interested in "old", "new" and "vanished"
    # From the states and parameters of these we construct the final state per service.
    cluster_items = {}
    for node in nodes:
        node_ipaddress = ip_lookup.lookup_ip_address(node)
        node_sources = _get_sources_for_discovery(node, node_ipaddress,
            check_plugin_names=sources.get_enforced_check_plugin_names(),
            do_snmp_scan=do_snmp_scan,
            on_error=on_error,
        )

        services = _get_discovered_services(node, node_ipaddress, node_sources, multi_host_sections, on_error)
        for (check_plugin_name, item), (check_source, paramstring) in services.items():
            descr = config.service_description(hostname, check_plugin_name, item)
            if hostname == config.host_of_clustered_service(node, descr):
                if (check_plugin_name, item) not in cluster_items:
                    cluster_items[(check_plugin_name, item)] = (check_source, paramstring)
                else:
                    first_check_source, first_paramstring = cluster_items[(check_plugin_name, item)]
                    if first_check_source == "old":
                        pass
                    elif check_source == "old":
                        cluster_items[(check_plugin_name, item)] = (check_source, paramstring)
                    elif first_check_source == "vanished" and check_source == "new":
                        cluster_items[(check_plugin_name, item)] = ("old", first_paramstring)
                    elif check_source == "vanished" and first_check_source == "new":
                        cluster_items[(check_plugin_name, item)] = ("old", paramstring)
                    # In all other cases either both must be "new" or "vanished" -> let it be

    # Now add manual and active serivce and handle ignored services
    _merge_manual_services(cluster_items, hostname, on_error)
    return cluster_items


# Translates a parameter string (read from autochecks) to it's final value
# (according to the current configuration)
def resolve_paramstring(check_plugin_name, paramstring):
    check_context = config.get_check_context(check_plugin_name)
    # TODO: Can't we simply access check_context[paramstring]?
    return eval(paramstring, check_context, check_context)


# Get the list of service of a host or cluster and guess the current state of
# all services if possible
# TODO: Can't we reduce the duplicate code here? Check out the "checking" code
def get_check_preview(hostname, use_caches, do_snmp_scan, on_error):
    if config.is_cluster(hostname):
        ipaddress = None
    else:
        ipaddress = ip_lookup.lookup_ip_address(hostname)

    sources = _get_sources_for_discovery(hostname, ipaddress, check_plugin_names=None,
                                         do_snmp_scan=do_snmp_scan, on_error=on_error)

    multi_host_sections = _get_host_sections_for_discovery(sources, use_caches=use_caches)

    services = _get_host_services(hostname, ipaddress, sources, multi_host_sections, on_error)

    table = []
    for (check_plugin_name, item), (check_source, paramstring) in services.items():
        params = None
        exitcode = None
        perfdata = []
        if check_source not in [ 'legacy', 'active', 'custom' ]:
            # apply check_parameters
            try:
                if type(paramstring) == str:
                    params = resolve_paramstring(check_plugin_name, paramstring)
                else:
                    params = paramstring
            except Exception:
                raise MKGeneralException("Invalid check parameter string '%s'" % paramstring)

            try:
                descr = config.service_description(hostname, check_plugin_name, item)
            except Exception, e:
                if on_error == "raise":
                    raise
                elif on_error == "warn":
                    console.error("Invalid service description: %s\n" % e)
                else:
                    continue # ignore

            check_api_utils.set_service(check_plugin_name, descr)
            section_name = cmk_base.check_utils.section_name_of(check_plugin_name)

            if check_plugin_name not in config.check_info:
                continue # Skip not existing check silently

            try:
                try:
                    section_content = multi_host_sections.get_section_content(hostname, ipaddress, section_name, for_discovery=True)
                except MKParseFunctionError, e:
                    if cmk.debug.enabled() or on_error == "raise":
                        x = e.exc_info()
                        # re-raise the original exception to not destory the trace. This may raise a MKCounterWrapped
                        # exception which need to lead to a skipped check instead of a crash
                        raise x[0], x[1], x[2]
                    else:
                        raise
            except Exception, e:
                if cmk.debug.enabled():
                    raise
                exitcode = 3
                output = "Error: %s" % e

            # TODO: Move this to a helper function
            if section_content is None: # No data for this check type
                exitcode = 3
                output = "Received no data"

            if not section_content and cmk_base.check_utils.is_snmp_check(check_plugin_name) \
               and not config.check_info[check_plugin_name]["handle_empty_info"]:
                exitcode = 0
                output = "Received no data"

            item_state.set_item_state_prefix(check_plugin_name, item)

            if exitcode == None:
                check_function = config.check_info[check_plugin_name]["check_function"]
                if check_source != 'manual':
                    params = check_table.get_precompiled_check_parameters(hostname, item,
                                config.compute_check_parameters(hostname, check_plugin_name, item, params),
                                check_plugin_name)
                else:
                    params = check_table.get_precompiled_check_parameters(hostname, item,
                                                                          params, check_plugin_name)

                try:
                    item_state.reset_wrapped_counters()
                    result = checking.sanitize_check_result(check_function(item, params, section_content),
                                                            cmk_base.check_utils.is_snmp_check(check_plugin_name))
                    item_state.raise_counter_wrap()
                except item_state.MKCounterWrapped, e:
                    result = (None, "WAITING - Counter based check, cannot be done offline")
                except Exception, e:
                    if cmk.debug.enabled():
                        raise
                    result = (3, "UNKNOWN - invalid output from agent or error in check implementation")
                if len(result) == 2:
                    result = (result[0], result[1], [])
                exitcode, output, perfdata = result
        else:
            descr = item
            exitcode = None
            output = "WAITING - %s check, cannot be done offline" % check_source.title()
            perfdata = []

        if check_source == "active":
            params = resolve_paramstring(check_plugin_name, paramstring)

        if check_source in [ "legacy", "active", "custom" ]:
            checkgroup = None
            if config.service_ignored(hostname, None, descr):
                check_source = "%s_ignored" % check_source
        else:
            checkgroup = config.check_info[check_plugin_name]["group"]

        table.append((check_source, check_plugin_name, checkgroup, item, paramstring,
                      params, descr, exitcode, output, perfdata))

    return table


#.
#   .--Autochecks----------------------------------------------------------.
#   |            _         _             _               _                 |
#   |           / \  _   _| |_ ___   ___| |__   ___  ___| | _____          |
#   |          / _ \| | | | __/ _ \ / __| '_ \ / _ \/ __| |/ / __|         |
#   |         / ___ \ |_| | || (_) | (__| | | |  __/ (__|   <\__ \         |
#   |        /_/   \_\__,_|\__\___/ \___|_| |_|\___|\___|_|\_\___/         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Reading, parsing, writing, modifying autochecks files               |
#   '----------------------------------------------------------------------'

# Read autochecks, but do not compute final check parameters,
# also return a forth column with the raw string of the parameters.
# Returns a table with three columns:
# 1. check_plugin_name
# 2. item
# 3. parameter string, not yet evaluated!
# TODO: use store.load_data_from_file()
def parse_autochecks_file(hostname):
    def split_python_tuple(line):
        quote = None
        bracklev = 0
        backslash = False
        for i, c in enumerate(line):
            if backslash:
                backslash = False
                continue
            elif c == '\\':
                backslash = True
            elif c == quote:
                quote = None # end of quoted string
            elif c in [ '"', "'" ] and not quote:
                quote = c # begin of quoted string
            elif quote:
                continue
            elif c in [ '(', '{', '[' ]:
                bracklev += 1
            elif c in [ ')', '}', ']' ]:
                bracklev -= 1
            elif bracklev > 0:
                continue
            elif c == ',':
                value = line[0:i]
                rest = line[i+1:]
                return value.strip(), rest

        return line.strip(), None

    path = "%s/%s.mk" % (cmk.paths.autochecks_dir, hostname)
    if not os.path.exists(path):
        return []

    lineno = 0
    table = []
    for line in file(path):
        lineno += 1
        try:
            line = line.strip()
            if not line.startswith("("):
                continue

            # drop everything after potential '#' (from older versions)
            i = line.rfind('#')
            if i > 0: # make sure # is not contained in string
                rest = line[i:]
                if '"' not in rest and "'" not in rest:
                    line = line[:i].strip()

            if line.endswith(","):
                line = line[:-1]
            line = line[1:-1] # drop brackets

            # First try old format - with hostname
            parts = []
            while True:
                try:
                    part, line = split_python_tuple(line)
                    parts.append(part)
                except:
                    break
            if len(parts) == 4:
                parts = parts[1:] # drop hostname, legacy format with host in first column
            elif len(parts) != 3:
                raise Exception("Invalid number of parts: %d (%r)" % (len(parts), parts))

            checktypestring, itemstring, paramstring = parts

            item = eval(itemstring)
            # With Check_MK 1.2.7i3 items are now defined to be unicode strings. Convert
            # items from existing autocheck files for compatibility. TODO remove this one day
            if type(item) == str:
                item = config.decode_incoming_string(item)

            table.append((eval(checktypestring), item, paramstring))
        except:
            if cmk.debug.enabled():
                raise
            raise Exception("Invalid line %d in autochecks file %s" % (lineno, path))
    return table


def _has_autochecks(hostname):
    return os.path.exists(cmk.paths.autochecks_dir + "/" + hostname + ".mk")


def _remove_autochecks_file(hostname):
    filepath = cmk.paths.autochecks_dir + "/" + hostname + ".mk"
    try:
        os.remove(filepath)
    except OSError:
        pass


# FIXME TODO: Consolidate with automation.py automation_write_autochecks_file()
def _save_autochecks_file(hostname, items):
    if not os.path.exists(cmk.paths.autochecks_dir):
        os.makedirs(cmk.paths.autochecks_dir)
    filepath = "%s/%s.mk" % (cmk.paths.autochecks_dir, hostname)
    out = file(filepath, "w")
    out.write("[\n")
    for check_plugin_name, item, paramstring in items:
        out.write("  (%r, %r, %s),\n" % (check_plugin_name, item, paramstring))
    out.write("]\n")


def set_autochecks_of(hostname, new_items):
    # A Cluster does not have an autochecks file
    # All of its services are located in the nodes instead
    # So we cycle through all nodes remove all clustered service
    # and add the ones we've got from stdin
    if config.is_cluster(hostname):
        for node in config.nodes_of(hostname):
            new_autochecks = []
            existing = parse_autochecks_file(node)
            for check_plugin_name, item, paramstring in existing:
                descr = config.service_description(node, check_plugin_name, item)
                if hostname != config.host_of_clustered_service(node, descr):
                    new_autochecks.append((check_plugin_name, item, paramstring))

            for (check_plugin_name, item), paramstring in new_items.items():
                new_autochecks.append((check_plugin_name, item, paramstring))

            # write new autochecks file for that host
            _save_autochecks_file(node, new_autochecks)

        # Check whether or not the cluster host autocheck files are still
        # existant. Remove them. The autochecks are only stored in the nodes
        # autochecks files these days.
        _remove_autochecks_file(hostname)
    else:
        existing = parse_autochecks_file(hostname)
        # write new autochecks file, but take paramstrings from existing ones
        # for those checks which are kept
        new_autochecks = []
        for ct, item, paramstring in existing:
            if (ct, item) in new_items:
                new_autochecks.append((ct, item, paramstring))
                del new_items[(ct, item)]

        for (ct, item), paramstring in new_items.items():
            new_autochecks.append((ct, item, paramstring))

        # write new autochecks file for that host
        _save_autochecks_file(hostname, new_autochecks)


# Remove all autochecks of a host while being cluster-aware!
def remove_autochecks_of(hostname):
    removed = 0
    nodes = config.nodes_of(hostname)
    if nodes:
        for node in nodes:
            removed += _remove_autochecks_of_host(node)
    else:
        removed += _remove_autochecks_of_host(hostname)

    return removed


def _remove_autochecks_of_host(hostname):
    old_items = parse_autochecks_file(hostname)
    removed = 0
    new_items = []
    for check_plugin_name, item, paramstring in old_items:
        descr = config.service_description(hostname, check_plugin_name, item)
        if hostname != config.host_of_clustered_service(hostname, descr):
            new_items.append((check_plugin_name, item, paramstring))
        else:
            removed += 1
    _save_autochecks_file(hostname, new_items)
    return removed