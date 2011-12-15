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

import defaults, htmllib, config, wato
from lib import *
from mod_python import apache
import os, md5, md5crypt, crypt, time

class MKAuthException(MKGeneralException):
    pass

def site_cookie_name():
    name = os.path.dirname(defaults.url_prefix).replace('/', '_')
    return 'auth%s' % name

# Validate hashes taken from the htpasswd file. This method handles
# crypt() and md5 hashes. This should be the common cases in the
# used htpasswd files.
def password_valid(pwhash, password):
    if pwhash[:3] == '$1$':
        salt = pwhash.split('$', 3)[2]
        return pwhash == wato.encrypt_password(password, salt)
    else:
        #html.write(repr(pwhash + ' ' + crypt.crypt(password, pwhash)))
        return pwhash == crypt.crypt(password, pwhash[:2])

# Loads the contents of a valid htpasswd file into a dictionary
# and returns the dictionary
def load_htpasswd():
    creds = {}

    for line in open(defaults.htpasswd_file, 'r'):
        username, pwhash = line.split(':', 1)
        creds[username] = pwhash.rstrip('\n')

    return creds

# Reads the auth secret from a file. Creates the files if it does
# not exist. Having access to the secret means that one can issue valid
# cookies for the cookie auth.
# FIXME: Secret auch replizieren
def load_secret():
    secret_path = '%s/auth.secret' % os.path.dirname(defaults.htpasswd_file)
    if not os.path.exists(secret_path):
        secret = md5.md5(str(time.time())).hexdigest()
        file(secret_path, 'w').write(secret)
    else:
        secret = file(secret_path).read().strip()
    return secret

# Generates the hash to be added into the cookie value
def generate_hash(username, now, pwhash):
    secret = load_secret()
    return md5.md5(username + now + pwhash + secret).hexdigest()

def del_auth_cookie():
    name = site_cookie_name()
    if html.has_cookie(name):
        html.del_cookie(name)

def set_auth_cookie(username, pwhash):
    now = str(time.time())
    html.set_cookie(site_cookie_name(), username
                                        + ':' + now
                                        + ':' + generate_hash(username, now, pwhash))

def check_auth_cookie(cookie_name):
    username, issue_time, cookie_hash = html.cookie(cookie_name, '::').split(':', 2)

    # FIXME: Ablauf-Zeit des Cookies testen
    #max_cookie_age = 10
    #if float(issue_time) < time.time() - max_cookie_age:
    #    del_auth_cookie()
    #    return ''

    users = load_htpasswd()
    if not username in users:
        raise MKAuthException(_('Username is unknown'))
    pwhash = users[username]

    # Validate the hash
    if cookie_hash != generate_hash(username, issue_time, pwhash):
        raise MKAuthException(_('Invalid credentials'))

    # Once reached this the cookie is a good one. Renew it!
    # Do not renew if:
    # a) The _ajaxid var is set
    # b) A logout is requested
    if (html.req.myfile != 'logout' or html.has_var('_ajaxid')) \
       and cookie_name == site_cookie_name():
        set_auth_cookie(username, pwhash)

    # Return the authenticated username
    return username

def check_auth():
    for cookie_name in html.get_cookie_names():
        if cookie_name.startswith('auth_'):
            try:
                return check_auth_cookie(cookie_name)
            except Exception, e:
                #html.write('Exception occured while checking cookie %s' % cookie_name)
                #raise
                pass

    return ''


def do_login():
    # handle the sent login form
    err = None
    if html.var('_login'):
        try:
            username = html.var('_username', '')
            if username == '':
                raise MKUserError('_username', _('No username given.'))

            password = html.var('_password', '')
            if password == '':
                raise MKUserError('_password', _('No password given.'))

            origtarget = html.var('_origtarget')
            if not origtarget or origtarget.endswith("/logout.py"):
                origtarget = defaults.url_prefix + 'check_mk/'

            users = load_htpasswd()
            if username in users and password_valid(users[username], password):
                # The login succeeded! Now:
                # a) Set the auth cookie
                # b) Unset the login vars in further processing
                # c) Show the real requested page (No redirect needed)
                set_auth_cookie(username, users[username])

                # Use redirects for URLs or simply execute other handlers for
                # mulitsite modules
                if '/' in origtarget:
                    html.set_http_header('Location', origtarget)
                    raise apache.SERVER_RETURN, apache.HTTP_MOVED_TEMPORARILY
                else:
                    # Remove login vars to hide them from the next page handler
                    try:
                        del html.req.vars['_username']
                        del html.req.vars['_password']
                        del html.req.vars['_login']
                        del html.req.vars['_origtarget']
                    except:
                        pass

                    return (username, origtarget)
            else:
                raise MKUserError(None, _('Invalid credentials.'))
        except MKUserError, e:
            html.add_user_error(e.varname, e.message)
            return e.message

def page_login():
    result = do_login()
    if type(result) == tuple:
        return result # Successfull login

    if html.mobile:
        import mobile
        return mobile.page_login()

    else:
        return normal_login_page()

def normal_login_page():
    html.set_render_headfoot(False)
    html.header(_("Check_MK Multisite Login"), javascripts=[], stylesheets=["pages", "login"])

    origtarget = html.var('_origtarget', '')
    if not origtarget and not html.req.myfile == 'login':
        origtarget = html.req.uri

    html.write("<div id=login>")
    html.write("<div id=logo></div>")
    html.write("<h1>Check_MK Multisite</h2>")

    html.write('<div id=form>')

    if html.has_user_errors():
        html.show_user_errors()

    html.begin_form("login", method = 'POST', add_transid = False)
    html.hidden_field('_origtarget', htmllib.attrencode(origtarget))
    html.write('<p>')
    html.write("<label class=legend for=_username>%s</label><br />" % _('Username'))
    html.text_input("_username", size = 50)
    html.write('</p>')

    html.write('<p>')
    html.write("<label class=legend for=_password>%s</label><br />" % _('Password'))
    html.password_input("_password", size = 50)
    html.write('</p>')

    html.write('<p class=submit>')
    html.button("_login", _('Login'))
    html.write('</p>')
    html.write('</div>')

    html.write("<div id=foot>Version: %s - &copy; "
               "<a href=\"http://mathias-kettner.de\">Mathias Kettner</a></div>" % defaults.check_mk_version)
    html.write("</div>")
    html.set_focus('_username')
    html.end_form()

    html.footer()
    return apache.OK

def page_logout():
    # Remove eventual existing cookie
    del_auth_cookie()

    if config.auth_type == 'cookie':
        html.set_http_header('Location', defaults.url_prefix + 'check_mk/')
        raise apache.SERVER_RETURN, apache.HTTP_MOVED_TEMPORARILY
    else:
        # Implement HTTP logout with cookie hack
        if not html.has_cookie('logout'):
            html.set_http_header('WWW-Authenticate', 'Basic realm="%s"' % defaults.nagios_auth_name)
            html.set_cookie('logout', '1')
            raise apache.SERVER_RETURN, apache.HTTP_UNAUTHORIZED
        else:
            html.del_cookie('logout')
            html.set_http_header('Location', defaults.url_prefix + 'check_mk/')
            raise apache.SERVER_RETURN, apache.HTTP_MOVED_TEMPORARILY

def page_edit_profile():
    html.header(_("Edit user profile"))

    if not config.user_id:
        raise MKUserError(None, _('Not logged in.'))

    if not config.may('edit_profile') and not config.may('change_password'):
        raise MKAuthException(_("You are not allowed to edit your user profile."))

    if html.has_var('_save') and html.check_transaction():
        try:
            import wato
            users = wato.load_users()

            #
            # Profile edit (user options like language etc.)
            #
            if config.may('edit_profile'):
                # Set the users language if requested
                language = html.var('language')
                if language and language != config.get_language():
                    # Set custom language
                    users[config.user_id]['language'] = language

                elif not language and config.get_language('') != '':
                    # Remove the customized language
                    del users[config.user_id]['language']

            #
            # Change the password if requested
            #
            if config.may('change_password'):
                password  = html.var('password')
                password2 = html.var('password2', '')
                if password:
                    if password2 and password != password2:
                        raise MKUserError("password2", _("The both passwords do not match."))

                    users[config.user_id]['password'] = wato.encrypt_password(password)

            wato.save_users(users)
            html.message(_("Successfully updated user profile."))

            if password:
                html.write("<script type='text/javascript'>if(top) top.location.reload(); else document.location.reload();</script>")
        except MKUserError, e:
            html.add_user_error(e.varname, e.message)

    if html.has_user_errors():
        html.show_user_errors()

    html.begin_form("profile", method="POST")
    html.write("<table class=form>")

    html.write("<tr><td class=legend>")
    html.write(_("Username"))
    html.write("</td><td class=content>")
    html.write(config.user_id)
    html.write("</td></tr>")

    if config.may('edit_profile'):
        languages = get_languages()

        if languages:
            html.write("<tr><td class=legend>")
            html.write(_("Language"))
            html.write("</td><td class=content>")
            default_label = _('Default (%s)') % config.default_language
            languages = [ ('', default_label) ] + languages
            html.select("language", languages, config.get_language(default_label))
            html.set_focus("lang")
            html.write("</td></tr>")

    if config.may('change_password'):
        html.write("<tr><td class=legend>")
        html.write(_("Password"))
        html.write("</td><td class=content>")
        html.password_input('password')
        html.write("</td></tr>")

        html.write("<tr><td class=legend>")
        html.write(_("Password confirmation"))
        html.write("</td><td class=content>")
        html.password_input('password2')
        html.write("</td></tr>")

    # Save button
    html.write("<tr><td colspan=2 class=buttons>")
    html.button("_save", _("Save"))
    html.write("</td></tr>")

    html.write("</table>")
    html.hidden_fields()
    html.end_form()
    html.footer()

    return apache.OK
