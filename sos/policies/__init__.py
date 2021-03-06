from __future__ import with_statement

import os
import re
import platform
import time
import json
import fnmatch
import tempfile
import random
import string

from getpass import getpass
from pwd import getpwuid
from sos.utilities import (ImporterHelper,
                           import_module,
                           shell_out,
                           sos_get_command_output)
from sos.plugins import IndependentPlugin, ExperimentalPlugin
from sos import _sos as _
from sos import SoSOptions, _arg_names
from textwrap import fill
from six import print_
from six.moves import input

PRESETS_PATH = "/var/lib/sos/presets"

try:
    import requests
    REQUESTS_LOADED = True
except ImportError:
    REQUESTS_LOADED = False


def get_human_readable(size, precision=2):
    # Credit to Pavan Gupta https://stackoverflow.com/questions/5194057/
    suffixes = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    suffixindex = 0
    while size > 1024 and suffixindex < 4:
        suffixindex += 1
        size = size/1024.0
    return "%.*f%s" % (precision, size, suffixes[suffixindex])


def import_policy(name):
    policy_fqname = "sos.policies.%s" % name
    try:
        return import_module(policy_fqname, Policy)
    except ImportError:
        return None


def load(cache={}, sysroot=None):
    if 'policy' in cache:
        return cache.get('policy')

    import sos.policies
    helper = ImporterHelper(sos.policies)
    for module in helper.get_modules():
        for policy in import_policy(module):
            if policy.check():
                cache['policy'] = policy(sysroot=sysroot)

    if 'policy' not in cache:
        cache['policy'] = GenericPolicy()

    return cache['policy']


class InitSystem(object):
    """Encapsulates an init system to provide service-oriented functions to
    sos.

    This should be used to query the status of services, such as if they are
    enabled or disabled on boot, or if the service is currently running.
    """

    def __init__(self, init_cmd=None, list_cmd=None, query_cmd=None):

        self.services = {}

        self.init_cmd = init_cmd
        self.list_cmd = "%s %s" % (self.init_cmd, list_cmd) or None
        self.query_cmd = "%s %s" % (self.init_cmd, query_cmd) or None

    def is_enabled(self, name):
        """Check if given service name is enabled """
        if self.services and name in self.services:
            return self.services[name]['config'] == 'enabled'
        return False

    def is_disabled(self, name):
        """Check if a given service name is disabled """
        if self.services and name in self.services:
            return self.services[name]['config'] == 'disabled'
        return False

    def is_service(self, name):
        """Checks if the given service name exists on the system at all, this
        does not check for the service status
        """
        return name in self.services

    def is_running(self, name):
        """Checks if the given service name is in a running state.

        This should be overridden by initsystems that subclass InitSystem
        """
        # This is going to be primarily used in gating if service related
        # commands are going to be run or not. Default to always returning
        # True when an actual init system is not specified by policy so that
        # we don't inadvertantly restrict sosreports on those systems
        return True

    def load_all_services(self):
        """This loads all services known to the init system into a dict.
        The dict should be keyed by the service name, and contain a dict of the
        name and service status
        """
        pass

    def _query_service(self, name):
        """Query an individual service"""
        if self.query_cmd:
            try:
                return sos_get_command_output("%s %s" % (self.query_cmd, name))
            except Exception:
                return None
        return None

    def parse_query(self, output):
        """Parses the output returned by the query command to make a
        determination of what the state of the service is

        This should be overriden by anything that subclasses InitSystem
        """
        return output

    def get_service_names(self, regex):
        """Get a list of all services discovered on the system that match the
        given regex.
        """
        reg = re.compile(regex, re.I)
        return [s for s in self.services.keys() if reg.match(s)]

    def get_service_status(self, name):
        """Returns the status for the given service name along with the output
        of the query command
        """
        _default = {
            'name': name,
            'status': 'missing',
            'output': ''
        }
        if name not in self.services:
            return _default
        if 'status' in self.services[name]:
            # service status has been queried before, return existing info
            return self.services[name]
        svc = self._query_service(name)
        if svc is not None:
            self.services[name]['status'] = self.parse_query(svc['output'])
            self.services[name]['output'] = svc['output']
            return self.services[name]
        return _default


class SystemdInit(InitSystem):

    def __init__(self):
        super(SystemdInit, self).__init__(
            init_cmd='systemctl',
            list_cmd='list-unit-files --type=service',
            query_cmd='status'
        )
        self.load_all_services()

    def parse_query(self, output):
        for line in output.splitlines():
            if line.strip().startswith('Active:'):
                return line.split()[1]
        return 'unknown'

    def load_all_services(self):
        svcs = shell_out(self.list_cmd).splitlines()[1:]
        for line in svcs:
            try:
                name = line.split('.service')[0]
                config = line.split()[1]
                self.services[name] = {
                    'name': name,
                    'config': config
                }
            except IndexError:
                pass

    def is_running(self, name):
        svc = self.get_service_status(name)
        return svc['status'] == 'active'


class PackageManager(object):
    """Encapsulates a package manager. If you provide a query_command to the
    constructor it should print each package on the system in the following
    format::

        package name|package.version

    You may also subclass this class and provide a get_pkg_list method to
    build the list of packages and versions.
    """

    query_command = None
    verify_command = None
    verify_filter = None
    chroot = None
    files = None

    def __init__(self, chroot=None, query_command=None,
                 verify_command=None, verify_filter=None,
                 files_command=None):
        self.packages = {}
        self.files = []

        self.query_command = query_command if query_command else None
        self.verify_command = verify_command if verify_command else None
        self.verify_filter = verify_filter if verify_filter else None
        self.files_command = files_command if files_command else None

        if chroot:
            self.chroot = chroot

    def all_pkgs_by_name(self, name):
        """
        Return a list of packages that match name.
        """
        return fnmatch.filter(self.all_pkgs().keys(), name)

    def all_pkgs_by_name_regex(self, regex_name, flags=0):
        """
        Return a list of packages that match regex_name.
        """
        reg = re.compile(regex_name, flags)
        return [pkg for pkg in self.all_pkgs().keys() if reg.match(pkg)]

    def pkg_by_name(self, name):
        """
        Return a single package that matches name.
        """
        pkgmatches = self.all_pkgs_by_name(name)
        if (len(pkgmatches) != 0):
            return self.all_pkgs_by_name(name)[-1]
        else:
            return None

    def get_pkg_list(self):
        """Returns a dictionary of packages in the following
        format::

            {'package_name': {'name': 'package_name',
                              'version': 'major.minor.version'}}

        """
        if self.query_command:
            cmd = self.query_command
            pkg_list = shell_out(
                cmd, timeout=0, chroot=self.chroot
            ).splitlines()

            for pkg in pkg_list:
                if '|' not in pkg:
                    continue
                elif pkg.count("|") == 1:
                    name, version = pkg.split("|")
                    release = None
                elif pkg.count("|") == 2:
                    name, version, release = pkg.split("|")
                self.packages[name] = {
                    'name': name,
                    'version': version.split(".")
                }
                release = release if release else None
                self.packages[name]['release'] = release

        return self.packages

    def all_pkgs(self):
        """
        Return a list of all packages.
        """
        if not self.packages:
            self.packages = self.get_pkg_list()
        return self.packages

    def pkg_nvra(self, pkg):
        fields = pkg.split("-")
        version, release, arch = fields[-3:]
        name = "-".join(fields[:-3])
        return (name, version, release, arch)

    def all_files(self):
        """
        Returns a list of files known by the package manager
        """
        if self.files_command and not self.files:
            cmd = self.files_command
            files = shell_out(cmd, timeout=0, chroot=self.chroot)
            self.files = files.splitlines()
        return self.files

    def build_verify_command(self, packages):
        """build_verify_command(self, packages) -> str
            Generate a command to verify the list of packages given
            in ``packages`` using the native package manager's
            verification tool.

            The command to be executed is returned as a string that
            may be passed to a command execution routine (for e.g.
            ``sos_get_command_output()``.

            :param packages: a string, or a list of strings giving
                             package names to be verified.
            :returns: a string containing an executable command
                      that will perform verification of the given
                      packages.
            :returntype: str or ``NoneType``
        """
        if not self.verify_command:
            return None

        # The re.match(pkg) used by all_pkgs_by_name_regex() may return
        # an empty list (`[[]]`) when no package matches: avoid building
        # an rpm -V command line with the empty string as the package
        # list in this case.
        by_regex = self.all_pkgs_by_name_regex
        verify_list = filter(None, map(by_regex, packages))

        # No packages after regex match?
        if not verify_list:
            return None

        verify_packages = ""
        for package_list in verify_list:
            for package in package_list:
                if any([f in package for f in self.verify_filter]):
                    continue
                if len(verify_packages):
                    verify_packages += " "
                verify_packages += package
        return self.verify_command + " " + verify_packages


#: Constants for on-disk preset fields
DESC = "desc"
NOTE = "note"
OPTS = "args"


class PresetDefaults(object):
    """Preset command line defaults.
    """
    #: Preset name, used for selection
    name = None
    #: Human readable preset description
    desc = None
    #: Notes on preset behaviour
    note = None
    #: Options set for this preset
    opts = SoSOptions()

    #: ``True`` if this preset if built-in or ``False`` otherwise.
    builtin = True

    def __str__(self):
        """Return a human readable string representation of this
            ``PresetDefaults`` object.
        """
        return ("name=%s desc=%s note=%s opts=(%s)" %
                (self.name, self.desc, self.note, str(self.opts)))

    def __repr__(self):
        """Return a machine readable string representation of this
            ``PresetDefaults`` object.
        """
        return ("PresetDefaults(name='%s' desc='%s' note='%s' opts=(%s)" %
                (self.name, self.desc, self.note, repr(self.opts)))

    def __init__(self, name="", desc="", note=None, opts=SoSOptions()):
        """Initialise a new ``PresetDefaults`` object with the specified
            arguments.

            :param name: The name of the new preset
            :param desc: A description for the new preset
            :param note: Note for the new preset
            :param opts: Options set for the new preset
            :returns: The newly initialised ``PresetDefaults``
        """
        self.name = name
        self.desc = desc
        self.note = note
        self.opts = opts

    def write(self, presets_path):
        """Write this preset to disk in JSON notation.

            :param presets_path: the directory where the preset will be
                                 written.
        """
        if self.builtin:
            raise TypeError("Cannot write built-in preset")

        # Make dictionaries of PresetDefaults values
        odict = self.opts.dict()
        pdict = {self.name: {DESC: self.desc, NOTE: self.note, OPTS: odict}}

        if not os.path.exists(presets_path):
            os.makedirs(presets_path, mode=0o755)

        with open(os.path.join(presets_path, self.name), "w") as pfile:
            json.dump(pdict, pfile)

    def delete(self, presets_path):
        os.unlink(os.path.join(presets_path, self.name))


NO_PRESET = 'none'
NO_PRESET_DESC = 'Do not load a preset'
NO_PRESET_NOTE = 'Use to disable automatically loaded presets'

GENERIC_PRESETS = {
    NO_PRESET: PresetDefaults(name=NO_PRESET, desc=NO_PRESET_DESC,
                              note=NO_PRESET_NOTE, opts=SoSOptions())
    }


class Policy(object):

    msg = _("""\
This command will collect system configuration and diagnostic information \
from this %(distro)s system.

For more information on %(vendor)s visit:

  %(vendor_url)s

The generated archive may contain data considered sensitive and its content \
should be reviewed by the originating organization before being passed to \
any third party.

%(changes_text)s

%(vendor_text)s
""")

    distro = "Unknown"
    vendor = "Unknown"
    vendor_url = "http://www.example.com/"
    vendor_text = ""
    PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    default_scl_prefix = ""
    name_pattern = 'legacy'
    presets = {"": PresetDefaults()}
    presets_path = PRESETS_PATH
    _in_container = False
    _host_sysroot = '/'

    def __init__(self, sysroot=None):
        """Subclasses that choose to override this initializer should call
        super() to ensure that they get the required platform bits attached.
        super(SubClass, self).__init__(). Policies that require runtime
        tests to construct PATH must call self.set_exec_path() after
        modifying PATH in their own initializer."""
        self._parse_uname()
        self.case_id = None
        self.package_manager = PackageManager()
        self._valid_subclasses = []
        self.set_exec_path()
        self._host_sysroot = sysroot
        self.register_presets(GENERIC_PRESETS)

    def get_valid_subclasses(self):
        return [IndependentPlugin] + self._valid_subclasses

    def set_valid_subclasses(self, subclasses):
        self._valid_subclasses = subclasses

    def del_valid_subclasses(self):
        del self._valid_subclasses

    valid_subclasses = property(get_valid_subclasses,
                                set_valid_subclasses,
                                del_valid_subclasses,
                                "list of subclasses that this policy can "
                                "process")

    def check(self):
        """
        This function is responsible for determining if the underlying system
        is supported by this policy.
        """
        return False

    def in_container(self):
        """ Returns True if sos is running inside a container environment.
        """
        return self._in_container

    def host_sysroot(self):
        return self._host_sysroot

    def dist_version(self):
        """
        Return the OS version
        """
        pass

    def get_preferred_archive(self):
        """
        Return the class object of the prefered archive format for this
        platform
        """
        from sos.archive import TarFileArchive
        return TarFileArchive

    def get_archive_name(self):
        """
        This function should return the filename of the archive without the
        extension.

        This uses the policy's name_pattern attribute to determine the name.
        There are two pre-defined naming patterns - 'legacy' and 'friendly'
        that give names like the following:

        legacy - 'sosreport-tux.123456-20171224185433'
        friendly - 'sosreport-tux-mylabel-123456-2017-12-24-ezcfcop.tar.xz'

        A custom name_pattern can be used by a policy provided that it
        defines name_pattern using a format() style string substitution.

        Usable substitutions are:

            name  - the short hostname of the system
            label - the label given by --label
            case  - the case id given by --case-id or --ticker-number
            rand  - a random string of 7 alpha characters

        Note that if a datestamp is needed, the substring should be set
        in the name_pattern in the format accepted by strftime().

        """
        name = self.get_local_name().split('.')[0]
        case = self.case_id
        label = self.commons['cmdlineopts'].label
        date = ''
        rand = ''.join(random.choice(string.ascii_lowercase) for x in range(7))

        if self.name_pattern == 'legacy':
            nstr = "sosreport-{name}{case}{date}"
            case = '.' + case if case else ''
            date = '-%Y%m%d%H%M%S'
        elif self.name_pattern == 'friendly':
            nstr = "sosreport-{name}{label}{case}{date}-{rand}"
            case = '-' + case if case else ''
            label = '-' + label if label else ''
            date = '-%Y-%m-%d'
        else:
            nstr = self.name_pattern

        nstr = nstr.format(
            name=name,
            label=label,
            case=case,
            date=date,
            rand=rand
        )
        return self.sanitize_filename(time.strftime(nstr))

    # for some specific binaries like "xz", we need to determine package
    # providing it; that is policy specific. By default return the binary
    # name itself until particular policy overwrites it
    def _get_pkg_name_for_binary(self, binary):
        return binary

    def get_cmd_for_compress_method(self, method, threads):
        cmd = method
        # use fast compression if using xz or bz2
        if cmd != "gzip":
            cmd = "%s -2" % cmd
        # determine number of threads to use for compressing - applicable
        # only for xz and of version 5.2 or higher
        if cmd.startswith("xz"):
            try:
                xz_package = self._get_pkg_name_for_binary(method)
                xz_version = self.package_manager\
                                 .all_pkgs()[xz_package]["version"]
            except Exception as e:
                xz_version = [u'0']  # deal like xz version is really old
            if xz_version >= [u'5', u'2']:
                cmd = "%s -T%d" % (cmd, threads)
        return cmd

    def get_tmp_dir(self, opt_tmp_dir):
        if not opt_tmp_dir:
            return tempfile.gettempdir()
        return opt_tmp_dir

    def get_default_scl_prefix(self):
        return self.default_scl_prefix

    def match_plugin(self, plugin_classes):
        if len(plugin_classes) > 1:
            for p in plugin_classes:
                # Give preference to the first listed tagging class
                # so that e.g. UbuntuPlugin is chosen over DebianPlugin
                # on an Ubuntu installation.
                if issubclass(p, self.valid_subclasses[0]):
                    return p
        return plugin_classes[0]

    def validate_plugin(self, plugin_class, experimental=False):
        """
        Verifies that the plugin_class should execute under this policy
        """
        valid_subclasses = [IndependentPlugin] + self.valid_subclasses
        if experimental:
            valid_subclasses += [ExperimentalPlugin]
        return any(issubclass(plugin_class, class_) for
                   class_ in valid_subclasses)

    def pre_work(self):
        """
        This function is called prior to collection.
        """
        pass

    def post_work(self):
        """
        This function is called after the sosreport has been generated.
        """
        pass

    def pkg_by_name(self, pkg):
        return self.package_manager.pkg_by_name(pkg)

    def _parse_uname(self):
        (system, node, release,
         version, machine, processor) = platform.uname()
        self.system = system
        self.hostname = node
        self.release = release
        self.smp = version.split()[1] == "SMP"
        self.machine = machine

    def set_commons(self, commons):
        self.commons = commons

    def _set_PATH(self, path):
        os.environ['PATH'] = path

    def set_exec_path(self):
        self._set_PATH(self.PATH)

    def is_root(self):
        """This method should return true if the user calling the script is
        considered to be a superuser"""
        return (os.getuid() == 0)

    def get_preferred_hash_name(self):
        """Returns the string name of the hashlib-supported checksum algorithm
        to use"""
        return "md5"

    def display_results(self, archive, directory, checksum, archivestat=None):
        # Display results is called from the tail of SoSReport.final_work()
        #
        # Logging is already shutdown and all terminal output must use the
        # print() call.

        # make sure a report exists
        if not archive and not directory:
            return False

        self._print()

        if archive:
            self._print(_("Your sosreport has been generated and saved "
                          "in:\n  %s\n") % archive, always=True)
            self._print(_(" Size\t%s") %
                        get_human_readable(archivestat.st_size))
            self._print(_(" Owner\t%s") %
                        getpwuid(archivestat.st_uid).pw_name)
        else:
            self._print(_("Your sosreport build tree has been generated "
                          "in:\n  %s\n") % directory, always=True)
        if checksum:
            self._print(" " + self.get_preferred_hash_name() + "\t" + checksum)
            self._print()
            self._print(_("Please send this file to your support "
                        "representative."))
        self._print()

    def _print(self, msg=None, always=False):
        """A wrapper around print that only prints if we are not running in
        quiet mode"""
        if always or not self.commons['cmdlineopts'].quiet:
            if msg:
                print_(msg)
            else:
                print_()

    def get_msg(self):
        """This method is used to prepare the preamble text to display to
        the user in non-batch mode. If your policy sets self.distro that
        text will be substituted accordingly. You can also override this
        method to do something more complicated."""
        if self.commons['cmdlineopts'].allow_system_changes:
            changes_text = "Changes CAN be made to system configuration."
        else:
            changes_text = "No changes will be made to system configuration."
        width = 72
        _msg = self.msg % {'distro': self.distro, 'vendor': self.vendor,
                           'vendor_url': self.vendor_url,
                           'vendor_text': self.vendor_text,
                           'tmpdir': self.commons['tmpdir'],
                           'changes_text': changes_text}
        _fmt = ""
        for line in _msg.splitlines():
            _fmt = _fmt + fill(line, width, replace_whitespace=False) + '\n'
        return _fmt

    def register_presets(self, presets, replace=False):
        """Add new presets to this policy object.

            Merges the presets dictionary ``presets`` into this ``Policy``
            object, or replaces the current presets if ``replace`` is
            ``True``.

            ``presets`` should be a dictionary mapping ``str`` preset names
            to ``<class PresetDefaults>`` objects specifying the command
            line defaults.

            :param presets: dictionary of presets to add or replace
            :param replace: replace presets rather than merge new presets.
        """
        if replace:
            self.presets = {}
        self.presets.update(presets)

    def find_preset(self, preset):
        """Find a preset profile matching the specified preset string.

            :param preset: a string containing a preset profile name.
            :returns: a matching PresetProfile.
        """
        # FIXME: allow fuzzy matching?
        for match in self.presets.keys():
            if match == preset:
                return self.presets[match]

        return None

    def probe_preset(self):
        """Return a ``PresetDefaults`` object matching the runing host.

            Stub method to be implemented by derived policy classes.

            :returns: a ``PresetDefaults`` object.
        """
        return self.presets[NO_PRESET]

    def load_presets(self, presets_path=None):
        """Load presets from disk.

            Read JSON formatted preset data from the specified path,
            or the default location at ``/var/lib/sos/presets``.

            :param presets_path: a directory containing JSON presets.
        """
        presets_path = presets_path or self.presets_path
        if not os.path.exists(presets_path):
            return
        for preset_path in os.listdir(presets_path):
            preset_path = os.path.join(presets_path, preset_path)

            with open(preset_path) as pf:
                try:
                    preset_data = json.load(pf)
                except ValueError:
                    continue

            for preset in preset_data.keys():
                pd = PresetDefaults(preset, opts=SoSOptions())
                data = preset_data[preset]
                pd.desc = data[DESC] if DESC in data else ""
                pd.note = data[NOTE] if NOTE in data else ""

                if OPTS in data:
                    for arg in _arg_names:
                        if arg in data[OPTS]:
                            setattr(pd.opts, arg, data[OPTS][arg])
                pd.builtin = False
                self.presets[preset] = pd

    def add_preset(self, name=None, desc=None, note=None, opts=SoSOptions()):
        """Add a new on-disk preset and write it to the configured
            presets path.

            :param preset: the new PresetDefaults to add
        """
        presets_path = self.presets_path

        if not name:
            raise ValueError("Preset name cannot be empty")

        if name in self.presets.keys():
            raise ValueError("A preset with name '%s' already exists" % name)

        preset = PresetDefaults(name=name, desc=desc, note=note, opts=opts)
        preset.builtin = False
        self.presets[preset.name] = preset
        preset.write(presets_path)

    def del_preset(self, name=""):
        if not name or name not in self.presets.keys():
            raise ValueError("Unknown profile: '%s'" % name)

        preset = self.presets[name]

        if preset.builtin:
            raise ValueError("Cannot delete built-in preset '%s'" %
                             preset.name)

        preset.delete(self.presets_path)
        self.presets.pop(name)


class GenericPolicy(Policy):
    """This Policy will be returned if no other policy can be loaded. This
    should allow for IndependentPlugins to be executed on any system"""

    def get_msg(self):
        return self.msg % {'distro': self.system}


class LinuxPolicy(Policy):
    """This policy is meant to be an abc class that provides common
    implementations used in Linux distros"""

    distro = "Linux"
    vendor = "None"
    PATH = "/bin:/sbin:/usr/bin:/usr/sbin"
    init = None
    # _ prefixed class attrs are used for storing any vendor-defined defaults
    # the non-prefixed attrs are used by the upload methods, and will be set
    # to the cmdline/config file values, if provided. If not provided, then
    # those attrs will be set to the _ prefixed values as a fallback.
    # TL;DR Use _upload_* for policy default values, use upload_* when wanting
    # to actual use the value in a method/override
    _upload_url = None
    _upload_directory = '/'
    _upload_user = None
    _upload_password = None
    _use_https_streaming = False
    _preferred_hash_name = None
    upload_url = None
    upload_user = None
    upload_password = None

    def __init__(self, sysroot=None, init=None):
        super(LinuxPolicy, self).__init__(sysroot=sysroot)
        self.init_kernel_modules()

        if init is not None:
            self.init_system = init
        elif os.path.isdir("/run/systemd/system/"):
            self.init_system = SystemdInit()
        else:
            self.init_system = InitSystem()

    def get_preferred_hash_name(self):

        if self._preferred_hash_name:
            return self._preferred_hash_name

        checksum = "md5"
        try:
            fp = open("/proc/sys/crypto/fips_enabled", "r")
        except IOError:
            self._preferred_hash_name = checksum
            return checksum

        fips_enabled = fp.read()
        if fips_enabled.find("1") >= 0:
            checksum = "sha256"
        fp.close()
        self._preferred_hash_name = checksum
        return checksum

    def default_runlevel(self):
        try:
            with open("/etc/inittab") as fp:
                pattern = r"id:(\d{1}):initdefault:"
                text = fp.read()
                return int(re.findall(pattern, text)[0])
        except (IndexError, IOError):
            return 3

    def kernel_version(self):
        return self.release

    def host_name(self):
        return self.hostname

    def is_kernel_smp(self):
        return self.smp

    def get_arch(self):
        return self.machine

    def get_local_name(self):
        """Returns the name usd in the pre_work step"""
        return self.host_name()

    def sanitize_filename(self, name):
        return re.sub(r"[^-a-z,A-Z.0-9]", "", name)

    def init_kernel_modules(self):
        """Obtain a list of loaded kernel modules to reference later for plugin
        enablement and SoSPredicate checks
        """
        lines = shell_out("lsmod", timeout=0).splitlines()
        self.kernel_mods = [line.split()[0].strip() for line in lines]

    def pre_work(self):
        # this method will be called before the gathering begins

        cmdline_opts = self.commons['cmdlineopts']
        caseid = cmdline_opts.case_id if cmdline_opts.case_id else ""

        # Set the cmdline settings to the class attrs that are referenced later
        # The policy default '_' prefixed versions of these are untouched to
        # allow fallback
        self.upload_url = cmdline_opts.upload_url
        self.upload_user = cmdline_opts.upload_user
        self.upload_directory = cmdline_opts.upload_directory
        self.upload_password = cmdline_opts.upload_pass

        if not cmdline_opts.batch and not \
                cmdline_opts.quiet:
            try:
                if caseid:
                    self.case_id = caseid
                else:
                    self.case_id = input(_("Please enter the case id "
                                           "that you are generating this "
                                           "report for [%s]: ") % caseid)
                # Policies will need to handle the prompts for user information
                if cmdline_opts.upload or self.upload_url:
                    self.prompt_for_upload_user()
                    self.prompt_for_upload_password()
                self._print()
            except KeyboardInterrupt:
                self._print()
                raise

        if cmdline_opts.case_id:
            self.case_id = cmdline_opts.case_id

        return

    def prompt_for_upload_user(self):
        """Should be overridden by policies to determine if a user needs to
        be provided or not
        """
        if not self.upload_user and not self._upload_user:
            msg = "Please provide upload user for %s: " % self.get_upload_url()
            self.upload_user = input(_(msg))

    def prompt_for_upload_password(self):
        """Should be overridden by policies to determine if a password needs to
        be provided for upload or not
        """
        if ((not self.upload_password and not self._upload_password) and
                self.upload_user):
            msg = (
                "Please provide the upload password for %s: "
                % self.upload_user
            )
            self.upload_password = getpass(msg)

    def upload_archive(self, archive):
        """Entry point for sos attempts to upload the generated archive to a
        policy or user specified location.

        Curerntly there is support for HTTPS, SFTP, and FTP. HTTPS uploads are
        preferred for policy-defined defaults.

        Policies that need to override uploading methods should override the
        respective upload_https(), upload_sftp(), and/or upload_ftp() methods
        and should NOT override this method.

        In order to enable this for a policy, that policy needs to implement
        the following:

        Required:
            Class Attrs:
                _upload_url                 The default location to use. Note
                                            these MUST include protocol header
                _upload_user                Default username, if any else None
                _upload_password            Default password, if any else None
                _use_https_streaming        Set to True if the HTTPS endpoint
                                            supports streaming data

        Optional:
            Class Attrs:
                _upload_directory   Default FTP server directory, if any

            Methods:
                prompt_for_upload_user()    Determines if sos should prompt
                                            for a username or not.
                get_upload_user()           Determines if the default or a
                                            different username should be used
                get_upload_https_auth()     Format authentication data for
                                            HTTPS uploads
                get_upload_url_string()     If you want your policy to print
                                            a string other than the default URL
                                            for your vendor/distro, override
                                            this method

        """
        self.upload_archive = archive
        self.upload_url = self.get_upload_url()
        if not self.upload_url:
            raise Exception("No upload destination provided by policy or by "
                            "--upload-url")
        upload_func = self._determine_upload_type()
        print(_("Attempting upload to %s" % self.get_upload_url_string()))
        return upload_func()

    def _determine_upload_type(self):
        """Based on the url provided, determine what type of upload to attempt.

        Note that this requires users to provide a FQDN address, such as
        https://myvendor.com/api or ftp://myvendor.com instead of
        myvendor.com/api or myvendor.com
        """
        prots = {
            'ftp': self.upload_ftp,
            'sftp': self.upload_sftp,
            'https': self.upload_https
        }
        if '://' not in self.upload_url:
            raise Exception("Must provide protocol in upload URL")
        prot, url = self.upload_url.split('://')
        if prot not in prots.keys():
            raise Exception("Unsupported or unrecognized protocol: %s" % prot)
        return prots[prot]

    def get_upload_https_auth(self, user=None, password=None):
        """Formats the user/password credentials using basic auth
        """
        if not user:
            user = self.get_upload_user()
        if not password:
            password = self.get_upload_password()

        return requests.auth.HTTPBasicAuth(user, password)

    def get_upload_url(self):
        """Helper function to determine if we should use the policy default
        upload url or one provided by the user
        """
        return self.upload_url or self._upload_url

    def get_upload_url_string(self):
        """Used by distro policies to potentially change the string used to
        report upload location from the URL to a more human-friendly string
        """
        return self.get_upload_url()

    def get_upload_user(self):
        """Helper function to determine if we should use the policy default
        upload user or one provided by the user
        """
        return self.upload_user or self._upload_user

    def get_upload_password(self):
        """Helper function to determine if we should use the policy default
        upload password or one provided by the user
        """
        return self.upload_password or self._upload_password

    def upload_sftp(self):
        """Attempts to upload the archive to an SFTP location.

        Due to the lack of well maintained, secure, and generally widespread
        python libraries for SFTP, sos will shell-out to the system's local ssh
        installation in order to handle these uploads.

        Do not override this method with one that uses python-paramiko, as the
        upstream sos team will reject any PR that includes that dependency.
        """
        raise NotImplementedError("SFTP support is not yet implemented")

    def _upload_https_streaming(self, archive):
        """If upload_https() needs to use requests.put(), this method is used
        to provide streaming functionality

        Policies should override this method instead of the base upload_https()

        Positional arguments:
            :param archive:     The open archive file object
        """
        return requests.put(self.get_upload_url(), data=archive,
                            auth=self.get_upload_https_auth())

    def _get_upload_headers(self):
        """Define any needed headers to be passed with the POST request here
        """
        return {}

    def _upload_https_no_stream(self, archive):
        """If upload_https() needs to use requests.post(), this method is used
        to provide non-streaming functionality

        Policies should override this method instead of the base upload_https()

        Positional arguments:
            :param archive:     The open archive file object
        """
        files = {
            'file': (archive.name.split('/')[-1], archive,
                     self._get_upload_headers())
        }
        return requests.post(self.get_upload_url(), files=files,
                             auth=self.get_upload_https_auth())

    def upload_https(self):
        """Attempts to upload the archive to an HTTPS location.

        Policies may define whether this upload attempt should use streaming
        or non-streaming data by setting the `use_https_streaming` class
        attr to True
        """
        if not REQUESTS_LOADED:
            raise Exception("Unable to upload due to missing python requests "
                            "library")

        with open(self.upload_archive, 'rb') as arc:
            if not self._use_https_streaming:
                r = self._upload_https_no_stream(arc)
            else:
                r = self._upload_https_streaming(arc)
            if r.status_code != 201:
                if r.status_code == 401:
                    raise Exception(
                        "Authentication failed: invalid user credentials"
                    )
                raise Exception("POST request returned %s: %s"
                                % (r.status_code, r.reason))
            return True

    def upload_ftp(self, url=None, directory=None, user=None, password=None):
        """Attempts to upload the archive to either the policy defined or user
        provided FTP location.
        """
        try:
            import ftplib
            import socket
        except ImportError:
            # socket is part of the standard library, should only fail here on
            # ftplib
            raise Exception("missing python ftplib library")

        if not url:
            url = self.get_upload_url()
        if url is None:
            raise Exception("no FTP server specified by policy, use --upload-"
                            "url to specify a location")

        url = url.replace('ftp://', '')

        if not user:
            user = self.get_upload_user()

        if not password:
            password = self.get_upload_password()

        if not directory:
            directory = self._upload_directory

        try:
            session = ftplib.FTP(url, user, password)
            session.cwd(directory)
        except socket.gaierror:
            raise Exception("unable to connect to %s" % url)
        except ftplib.error_perm as err:
            errno = str(err).split()[0]
            if errno == 503:
                raise Exception("could not login as '%s'" % user)
            if errno == 550:
                raise Exception("could not set upload directory to %s"
                                % directory)

        try:
            with open(self.upload_archive, 'rb') as _arcfile:
                session.storbinary(
                    "STOR %s" % self.upload_archive.split('/')[-1],
                    _arcfile
                )
            session.quit()
            return True
        except IOError:
            raise Exception("could not open archive file")

# vim: set et ts=4 sw=4 :
