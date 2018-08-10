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

import traceback
import json

import cmk

import cmk.gui.pages
from cmk.gui.log import logger
import cmk.gui.utils as utils
import cmk.gui.config as config
import cmk.gui.watolib as watolib
import cmk.gui.userdb as userdb
import cmk.gui.i18n
from cmk.gui.i18n import _
from cmk.gui.globals import html
from cmk.gui.exceptions import MKGeneralException, MKUserError, MKAuthException, \
                           MKException
from cmk.gui.valuespec import *

import cmk.gui.plugins.webapi

if not cmk.is_raw_edition():
    import cmk.gui.cee.plugins.webapi

if cmk.is_managed_edition():
    import cmk.gui.cme.managed as managed
else:
    managed = None

# TODO: Kept for compatibility reasons with legacy plugins
from cmk.gui.plugins.webapi.utils import (
    api_actions,
    add_configuration_hash,
    validate_request_keys,
    validate_config_hash,
    validate_host_attributes,
    check_hostname,
)


loaded_with_language = False

def load_plugins(force):
    global loaded_with_language
    if loaded_with_language == cmk.gui.i18n.get_current_language() and not force:
        return

    utils.load_web_plugins("webapi", globals())

    # This must be set after plugin loading to make broken plugins raise
    # exceptions all the time and not only the first time (when the plugins
    # are loaded).
    loaded_with_language = cmk.gui.i18n.get_current_language()

    config.declare_permission("wato.api_allowed", _("Access to Web-API"),
                                                  _("This permissions specifies if the role "\
                                                    "is able to use Web-API functions. It is only available "\
                                                    "for automation users."),
                              config.builtin_role_ids)


@cmk.gui.pages.register("webapi")
def page_api():
    try:
        # The API uses JSON format by default and python as optional alternative
        output_format = html.var("output_format", "json")
        if output_format not in [ "json", "python" ]:
            raise MKUserError(None, "Only \"json\" and \"python\" are supported as output formats")
        else:
            html.set_output_format(output_format)

        if not config.user.get_attribute("automation_secret"):
            raise MKAuthException("The WATO API is only available for automation users")

        if not config.wato_enabled:
            raise MKUserError(None, _("WATO is disabled on this site."))

        config.user.need_permission("wato.use")
        config.user.need_permission("wato.api_allowed")


        action = html.var('action')
        if action not in api_actions:
            raise MKUserError(None, "Unknown API action %s" % html.attrencode(action))


        for permission in api_actions[action].get("required_permissions", []):
            config.user.need_permission(permission)

        # Initialize host and site attributes
        watolib.init_watolib_datastructures()

        # Prepare request_object
        # Most of the time the request is given as json
        # However, the plugin may have an own mechanism to interpret the request
        request_object = {}
        if api_actions[action].get("dont_eval_request"):
            if html.var("request"):
                request_object = html.var("request")
        else:
            request_object = html.get_request(exclude_vars=["action"])


        # Check if the data was sent with the correct data format
        # Some API calls only allow python code
        # TODO: convert the api_action dict into an object which handles the validation
        required_input_format = api_actions[action].get("required_input_format")
        if required_input_format:
            if required_input_format != request_object["request_format"]:
                raise MKUserError(None, "This API call requires a %s-encoded request parameter" % required_input_format)

        required_output_format = api_actions[action].get("required_output_format")
        if required_output_format:
            if required_output_format != html.output_format:
                raise MKUserError(None, "This API call requires the parameter output_format=%s" % required_output_format)


        # The request_format parameter is not forwarded into the API action
        if "request_format" in request_object:
            del request_object["request_format"]

        if api_actions[action].get("locking", True):
            watolib.lock_exclusive() # unlock is done automatically

        if watolib.is_read_only_mode_enabled() and not watolib.may_override_read_only_mode():
            raise MKUserError(None, watolib.read_only_message())

        action_response = api_actions[action]["handler"](request_object)
        response = { "result_code": 0, "result": action_response }

    except MKAuthException, e:
        response = { "result_code": 1, "result": _("Authorization Error. Insufficent permissions for '%s'") % e }
    except MKException, e:
        response = { "result_code": 1, "result": _("Check_MK exception: %s") % e }
    except Exception, e:
        if config.debug:
            raise
        logger.exception()
        response = {
            "result_code" : 1,
            "result"      : _("Unhandled exception: %s") % traceback.format_exc(),
        }

    if html.output_format == "json":
        html.write(json.dumps(response))
    else:
        html.write(repr(response))