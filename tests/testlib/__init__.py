#!/usr/bin/env python
# encoding: utf-8

import os
import pwd
import time
import pytest
import platform
import re
import requests
import socket
import pipes
import subprocess

from urlparse import urlparse
from BeautifulSoup import BeautifulSoup

try:
    import simplejson as json
except ImportError:
    import json

def repo_path():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def cmk_path():
    return repo_path()


def cmc_path():
    return os.path.realpath(repo_path() + "/../cmc")


# Directory for persisting variable data produced by tests
def var_dir():
    if "WORKSPACE" in os.environ:
        base_dir = os.environ["WORKSPACE"]
    else:
        base_dir = repo_path() + "/tests"

    return base_dir + "/var"


def omd(cmd):
    return os.system(" ".join(["sudo", "/usr/bin/omd"] + cmd + [">/dev/null"])) >> 8


# It's ok to make it currently only work on debian based distros
class CMKVersion(object):
    DEFAULT = "default"
    DAILY   = "daily"

    CEE   = "cee"
    CRE   = "cre"

    def __init__(self, version, edition):
        self.set_version(version)

        if len(edition) != 3:
            raise Exception("Invalid edition: %s. Must be short notation (cee, cre, ...)")
        self.edition_short = edition

        self._credentials = ("d-vonheute", "lOBFsgAH")


    def get_default_version(self):
        if os.path.exists("/etc/alternatives/omd"):
            path = os.readlink("/etc/alternatives/omd")
        else:
            path = os.readlink("/omd/versions/default")
        return os.path.split(path)[-1].rsplit(".", 1)[0]


    def set_version(self, version):
        if version == CMKVersion.DAILY:
            self.version = time.strftime("%Y.%m.%d")

        elif version == CMKVersion.DEFAULT:
            self.version = self.get_default_version()

        else:
            if ".cee" in version or ".cre" in version:
                raise Exception("Invalid version. Remove the edition suffix!")
            self.version = version


    def edition(self):
        return self.edition_short == CMKVersion.CRE and "raw" or "enterprise"


    def _needed_distro(self):
        return os.popen("lsb_release -a 2>/dev/null | grep Codename | awk '{print $2}'").read().strip()


    def _needed_architecture(self):
        return platform.architecture()[0] == "64bit" and "amd64" or "i386"


    def package_name(self):
        return "check-mk-%s-%s_0.%s_%s.deb" % \
		(self.edition(), self.version, self._needed_distro(), self._needed_architecture())


    def package_url(self):
        return "https://mathias-kettner.de/support/%s/%s" % (self.version, self.package_name())


    def version_directory(self):
        return "%s.%s" % (self.version, self.edition_short)


    def version_path(self):
        return "/omd/versions/%s" % self.version_directory()


    def is_installed(self):
        return os.path.exists(self.version_path())


    def install(self):
        temp_package_path = "/tmp/%s" % self.package_name()

        print(self.package_url())
        response = requests.get(self.package_url(), auth=self._credentials, verify=False)
        if response.status_code != 200:
            raise Exception("Failed to load package: %s" % self.package_url())
        file(temp_package_path, "w").write(response.content)

        cmd = "sudo /usr/bin/gdebi --non-interactive %s" % temp_package_path
        print(cmd)
        if os.system(cmd) >> 8 != 0:
            raise Exception("Failed to install package: %s" % temp_package_path)

        assert self.is_installed()



class Site(object):
    def __init__(self, site_id, reuse=True, version=CMKVersion.DEFAULT,
                 edition=CMKVersion.CEE):
        assert site_id
        self.id      = site_id
        self.root    = "/omd/sites/%s" % self.id
        self.version = CMKVersion(version, edition)

        self.reuse   = reuse

        self.http_proto   = "http"
        self.http_address = "127.0.0.1"
        self.url          = "%s://%s/%s/check_mk/" % (self.http_proto, self.http_address, self.id)

        self._gather_livestatus_port()


    @property
    def livestatus_port(self):
        if self._livestatus_port == None:
            raise Exception("Livestatus TCP not opened yet")
        return self._livestatus_port


    @property
    def live(self):
        import livestatus
        live = livestatus.SingleSiteConnection("tcp:127.0.0.1:%d" %
                                                     self.livestatus_port)
        live.set_timeout(2)
        return live


    def execute(self, cmd, *args, **kwargs):
        assert type(cmd) == list, "The command must be given as list"

        cmd = [ "sudo", "su", "-l", self.id,
                "-c", pipes.quote(" ".join([ pipes.quote(p) for p in cmd ])) ]
        cmd_txt = " ".join(cmd)
        return subprocess.Popen(cmd_txt, shell=True, *args, **kwargs)


    def cleanup_if_wrong_version(self):
        if not self.exists():
            return

        if self.current_version_directory() == self.version.version_directory():
            return

        # Now cleanup!
        self.rm()


    def current_version_directory(self):
        return os.path.split(os.readlink("/omd/sites/%s/version" % self.id))[-1]


    def create(self):
        if not self.version.is_installed():
            self.version.install()

        if not self.reuse and self.exists():
            raise Exception("The site %s already exists." % self.id)

        if not self.exists():
            assert omd(["-V", self.version.version_directory(), "create", self.id]) == 0
            assert os.path.exists("/omd/sites/%s" % self.id)


    def rm_if_not_reusing(self):
        if not self.reuse:
            self.rm()


    def rm(self):
        assert omd(["-f", "rm", "--kill", self.id]) == 0


    def start(self):
        if not self.is_running():
            assert omd(["start", self.id]) == 0
            if not self.is_running():
                raise Exception("The site %s is not running completely after starting" % self.id)


    def exists(self):
        return os.path.exists("/omd/sites/%s" % self.id)


    def is_running(self):
        return omd(["status", "--bare", self.id]) == 0


    def set_config(self, key, val):
        assert omd(["config", self.id, "set", key, val]) == 0


    def get_config(self, key):
        p = self.execute(["omd", "config", "show", key], stdout=subprocess.PIPE)
        return p.communicate()[0].strip()


    def prepare_for_tests(self):
        self.init_wato()


    def init_wato(self):
        web = CMKWebSession(self)
        web.login()
        web.get("wato.py")
        assert os.path.exists(self.root + "/etc/check_mk/conf.d/wato/rules.mk"), \
            "Failed to initialize WATO data structures"


    # This opens a currently free TCP port and remembers it in the object for later use
    # Not free of races, but should be sufficient.
    def open_livestatus_tcp(self):
        if self.is_running():
            return

        self.set_config("LIVESTATUS_TCP", "on")
        self.set_config("LIVESTATUS_TCP_PORT", str(self._livestatus_port))


    def _gather_livestatus_port(self):
        if self.reuse and self.exists():
            port = int(self.get_config("LIVESTATUS_TCP_PORT"))
        else:
            port = 9123
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            while sock.connect_ex(('127.0.0.1', port)) == 0:
                port += 1

        self._livestatus_port = port


    # Problem: The group change only affects new sessions of the test_user
    #def add_test_user_to_site_group(self):
    #    test_user = pwd.getpwuid(os.getuid())[0]

    #    if os.system("sudo usermod -a -G %s %s" % (self.id, test_user)) >> 8 != 0:
    #        raise Exception("Failed to add test user \"%s\" to site group")



class WebSession(requests.Session):
    transids = {}

    def __init__(self, ident):
        self.ident = ident
        super(WebSession, self).__init__()


    def check_redirect(self, path, proto="http", expected_code=302, expected_target=None):
        url = self.url(proto, path)

        response = self.get(path, expected_code=expected_code, allow_redirects=False)
        if expected_target:
            assert response.headers['Location'] == expected_target


    def get(self, path, proto="http", expected_code=200, allow_errors=False, **kwargs):
        url = self.url(proto, path)

        response = super(WebSession, self).get(url, **kwargs)
        self._handle_http_response(response, expected_code, allow_errors)
        return response


    def post(self, path, proto="http", expected_code=200, allow_errors=False, **kwargs):
        url = self.url(proto, path)

	if kwargs.get("add_transid"):
            del kwargs["add_transid"]

            transids = WebSession.transids.get(self.ident, [])
            if not transids:
                raise Exception('Tried to add a transid, but none available at the moment')

            if "?" in url:
                url += "&"
            else:
                url += "?"
            url += "_transid=" + transids.pop()

        response = super(WebSession, self).post(url, **kwargs)
        self._handle_http_response(response, expected_code, allow_errors)
        return response


    def _handle_http_response(self, response, expected_code, allow_errors):
        assert "Content-Type" in response.headers

        # TODO: Copied from CMA tests. Needed?
        # Apache error responses are sent as ISO-8859-1. Ignore these pages.
        #if r.status_code == 200 \
        #   and (not self._allow_wrong_encoding and not mime_type.startswith("image/")):
        #    assert r.encoding == "UTF-8", "Got invalid encoding (%s) for URL %s" % (r.encoding, r.url))

        mime_type = self._get_mime_type(response)

        assert response.status_code == expected_code, \
            "Got invalid status code (%d != %d) for URL %s (Location: %s)" % \
                  (response.status_code, expected_code,
                   response.url, response.headers.get('Location', "None"))

        if mime_type == "text/html":
            self._check_html_page(response, allow_errors)


    def _get_mime_type(self, response):
        return response.headers["Content-Type"].split(";", 1)[0]


    def _check_html_page(self, response, allow_errors):
        self._extract_transids(response.text)
        self._find_errors(response.text, allow_errors)
        self._check_html_page_resources(response)


    def _extract_transids(self, body):
        # Extract transids from the pages to be used in later actions
        # issued by the tests
        matches = re.findall('name="_transid" value="([^"]+)"', body)
        matches += re.findall('_transid=([0-9/]+)', body)
        for match in matches:
            transids = WebSession.transids.setdefault(self.ident, [])
            transids.append(match)


    def _find_errors(self, body, allow_errors):
        matches = re.search('<div class=error>(.*?)</div>', body, re.M | re.DOTALL)
        if allow_errors and matches:
            print "Found error message, but it's allowed: %s" % matches.groups()
        else:
            assert not matches, "Found error message: %s" % matches.groups()


    def _check_html_page_resources(self, response):
        soup = BeautifulSoup(response.text)

        base_url = urlparse(response.url).path
        if ".py" in base_url:
            base_url = os.path.dirname(base_url)

        # There might be other resources like iframe, audio, ... but we don't care about them

        for img_url in self._find_resource_urls("img", "src", soup):
            assert not img_url.startswith("/")
            req = self.get(base_url + "/" + img_url)

            mime_type = self._get_mime_type(req)
            assert mime_type in [ "image/png" ]

        for script_url in self._find_resource_urls("script", "src", soup):
            assert not script_url.startswith("/")
            req = self.get(base_url + "/" + script_url)

            mime_type = self._get_mime_type(req)
            assert mime_type in [ "application/javascript" ]

        for css_url in self._find_resource_urls("link", "href", soup, filters=[("rel", "stylesheet")]):
            assert not css_url.startswith("/")
            req = self.get(base_url + "/" + css_url)

            mime_type = self._get_mime_type(req)
            assert mime_type in [ "text/css" ]

        for url in self._find_resource_urls("link", "href", soup, filters=[("rel", "shortcut icon")]):
            assert not url.startswith("/")
            req = self.get(base_url + "/" + url)

            mime_type = self._get_mime_type(req)
            assert mime_type in [ "image/vnd.microsoft.icon" ]


    def _find_resource_urls(self, tag, attribute, soup, filters=[]):
        urls = []

        for element in soup.findAll(tag):
            try:
                skip = False
                for attr, val in filters:
                    if element[attr] != val:
                        skip = True
                        break

                if not skip:
                    urls.append(element[attribute])
            except KeyError:
                pass

        return urls


    def login(self, username=None, password=None):
        raise NotImplementedError()


    def logout(self):
        raise NotImplementedError()



class CMKWebSession(WebSession):

    def __init__(self, site):
        self.site = site
        super(CMKWebSession, self).__init__(site.id)


    # Computes a full URL inkl. http://... from a URL starting with the path.
    def url(self, proto, path):
        # In case no path component is in URL, add the path to the "/[site]/check_mk"
        if "/" not in urlparse(path).path:
            path = "/%s/check_mk/%s" % (self.site.id, path)

        return '%s://%s%s' % (self.site.http_proto, self.site.http_address, path)


    def login(self, username="omdadmin", password="omd"):
        login_page = self.get("").text
        assert "_username" in login_page, "_username not found on login page - page broken?"
        assert "_password" in login_page
        assert "_login" in login_page

        r = self.post("login.py", data={
            "filled_in" : "login",
            "_username" : username,
            "_password" : password,
            "_login"    : "Login",
        })
        auth_cookie = r.cookies.get("auth_%s" % self.site.id)
        assert auth_cookie
        assert auth_cookie.startswith("%s:" % username)

        assert "side.py" in r.text
        assert "dashboard.py" in r.text


    def set_language(self, lang):
        lang = "" if lang == "en" else lang

        profile_page = self.get("user_profile.py").text
        assert "name=\"language\"" in profile_page
        assert "value=\""+lang+"\"" in profile_page

        r = self.post("user_profile.py", data={
            "filled_in" : "profile",
            "_set_lang" : "on",
            "language"  : lang,
            "_save"     : "Save",
        }, add_transid=True)

        if lang == "":
            assert "Successfully updated" in r.text
        else:
            assert "Benutzerprofil erfolgreich aktualisiert" in r.text


    def logout(self):
        r = self.get("logout.py")
        assert "action=\"login.py\"" in r.text


    #
    # Web-API for managing hosts, services etc.
    #

    def _automation_credentials(self):
        secret_path = "%s/var/check_mk/web/cmkautomation/automation.secret" % self.site.root
        p = self.site.execute(["cat", secret_path], stdout=subprocess.PIPE)
        secret = p.communicate()[0].rstrip()
        if secret == "":
            raise Exception("Failed to read secret from %s" % secret_path)

        return {
            "_username" : "cmkautomation",
            "_secret"   : secret,
        }


    def _api_request(self, url, data):
        data.update(self._automation_credentials())

        req = self.post(url, data=data)
        response = json.loads(req.text)

        assert response["result_code"] == 0, \
               "An error occured: %s" % response["result"]

        return response["result"]


    def add_host(self, hostname, folder="/", attributes=None):
        result = self._api_request("webapi.py?action=add_host", {
            "request": json.dumps({
                "hostname"   : hostname,
                "folder"     : folder,
                "attributes" : attributes or {},
            }),
        })

        assert result == None

        host = self.get_host(hostname)

        assert host["hostname"] == hostname
        assert host["path"] == ""
        assert host["attributes"] == attributes


    def get_host(self, hostname):
        result = self._api_request("webapi.py?action=get_host", {
            "request": json.dumps({
                "hostname"   : hostname,
            }),
        })

        assert type(result) == dict
        assert "hostname" in result
        assert "path" in result
        assert "attributes" in result

        return result


    def get_all_hosts(self, effective_attributes=0):
        result = self._api_request("webapi.py?action=get_all_hosts", {
            "request": json.dumps({
                "effective_attributes": effective_attributes,
            }),
        })

        assert type(result) == dict
        return result


    def delete_host(self, hostname):
        result = self._api_request("webapi.py?action=delete_host", {
            "request": json.dumps({
                "hostname"   : hostname,
            }),
        })

        assert result == None

        hosts = self.get_all_hosts(hostname)
        assert hostname not in hosts


    def discover_services(self, hostname, mode=None):
        request = {
            "hostname"   : hostname,
        }

        if mode != None:
            request["mode"] = mode

        result = self._api_request("webapi.py?action=discover_services", {
            "request": json.dumps(request),
        })

        assert type(result) == unicode
        assert result.startswith("Service discovery successful")


    def activate_changes(self, mode=None, foreign_changes=None):
        request = {}

        if mode != None:
            request["mode"] = mode

        if foreign_changes != None:
            request["foreign_changes"] = foreign_changes

        result = self._api_request("webapi.py?action=activate_changes", {
            "request": json.dumps(request),
        })

        assert result == None


@pytest.fixture(scope="module")
def web(site):
    web = CMKWebSession(site)
    web.login()
    web.set_language("en")
    return web
