import sys
import re
import subprocess
import os
import socket
import datetime
import stat

import netifaces
import yaml
import yum


class YumDeps(object):
    NAME_VERSION_SEPARATOR = '/'

    def __init__(self, yumbase):
        self.yumbase = yumbase
        self.requires = YumDeps._get_requires(yumbase)
        self.whatrequires = YumDeps._get_whatrequires(self.requires)
        self.all_updates = None


    @classmethod
    def get_id(clazz, pkg):
        if pkg.epoch and pkg.epoch != '0':
            return '%s%s%s:%s-%s.%s' % (pkg.name, YumDeps.NAME_VERSION_SEPARATOR, pkg.epoch, pkg.version, pkg.release, pkg.arch)
        return '%s%s%s-%s.%s' % (pkg.name, YumDeps.NAME_VERSION_SEPARATOR, pkg.version, pkg.release, pkg.arch)


    @classmethod
    def _get_requires(clazz, yumbase):
        requires = {}
        for pkg in yumbase.rpmdb.returnPackages():
            id = YumDeps.get_id(pkg)
            requires[id] = set()
            for dep in pkg.requiresList():
                if dep.startswith('rpmlib'):
                    continue
                installeds = yumbase.returnInstalledPackagesByDep(dep)
                requires[id].update(map(YumDeps.get_id, installeds))
            requires[id] = list(requires[id])
        return requires


    @classmethod
    def _get_whatrequires(clazz, requires):
        whatrequires = {}
        for pkg, requireds in requires.iteritems():
            for required in requireds:
                if required not in whatrequires:
                    whatrequires[required] = []
                whatrequires[required].append(pkg)
        return whatrequires


    def get_service_artefact(self, service_file):
        sas = self.yumbase.rpmdb.getProvides(service_file)
        if not sas:
            return None
        if len(sas) > 1:
            sys.stderr.write('ERROR: %(service_file)s cannot be mapped to exactly one package: %(sas)s\n' % locals())
            return None
        return YumDeps.get_id(sas.keys()[0])


    def get_all_whatrequires(self, artefact, visited=None):
        if not visited:
            visited = set()
        if artefact in visited:
            return []
        visited.add(artefact)
        requires = self.whatrequires.get(artefact, [])
        result = [artefact]
        for require in requires:
            result.extend(self.get_all_whatrequires(require, visited))
        return result


    def get_all_requires(self, artefacts, visited=None):
        if not visited:
            visited = set()
        result = []
        for artefact in artefacts:
            if artefact in visited:
                continue
            visited.add(artefact)
            result.append(artefact)
            result.extend(self.get_all_requires(self.requires.get(artefact), visited))
        return result


    def strip_version(self, pkg):
        return pkg.split(YumDeps.NAME_VERSION_SEPARATOR, 1)[0]


    def load_all_updates(self):
        if not self.all_updates:
            self.all_updates = {}
            ups = yum.rpmUtils.updates.Updates(
                self.yumbase.rpmdb.simplePkgList(), self.yumbase.pkgSack.simplePkgList())
            ups.doUpdates()
            ups.condenseUpdates()
            for up in ups.getUpdatesTuples():
                new_pkg = YumDeps.get_id(self.yumbase.getPackageObject(up[0]))
                try:
                    old_pkg = YumDeps.get_id(self.yumbase.getPackageObject(up[1]))
                except yum.Errors.DepError:
                    old_pkg = up[1]
                self.all_updates[new_pkg] = old_pkg
        return self.all_updates



class Status(object):
    def __init__(self, yadt_default_filename='/etc/default/yadt'):
        self.yumbase = yum.YumBase()
        is_root = os.geteuid() == 0
        self.yumbase.preconf.init_plugins = is_root
        self.yumbase.conf.cache = not(is_root)

        #self.defaults = Status.load_defaults(yadt_default_filename)
        self.defaults = {
            'YADT_ARTEFACT_FILTER': '.*',
            'YADT_SERVICES_FILE': 'res/yadt.services.dummy',
            'YADT_YUM_COMMAND': 'sudo',
            'YADT_LOCK_DIR': '/var/lock/yadt'
        }
        self.artefacts_filter = re.compile(self.defaults.get('YADT_ARTEFACT_FILTER', '')).match

        yadt_services_filename = self.defaults.get('YADT_SERVICES_FILE')
        try:
            file = open(os.path.join(os.getcwd(), yadt_services_filename), 'r')
            self.service_defs = yaml.load(file)
            file.close()
        except IOError, e:
            self.service_defs = set()
            print >> sys.stderr, 'cannot find %s, skipping service handling' % yadt_services_filename
            print >> sys.stderr, e

        self.lockstate = self.get_lock_state()

        self.yumdeps = YumDeps(self.yumbase)

        self.services = []
        for service_def in self.service_defs:
            name = service_def.keys()[0]
            service = service_def.get(name)
            if service:
                service = service.copy()
            else:
                service = {}
            service['name'] = name
            init_script = '/etc/init.d/%s' % name
            artefact = self.yumdeps.get_service_artefact(init_script)
            if artefact:
                service['init_script'] = init_script
                service['service_artefact'] = artefact
                whatrequires = self.yumdeps.get_all_whatrequires(artefact)
                service['toplevel_artefacts'] = whatrequires
                service['needs_artefacts'] = map(self.yumdeps.strip_version, filter(
                    self.artefacts_filter, self.yumdeps.get_all_requires(whatrequires)))
            else:
                service['state_handling'] = 'serverside'
            self.services.append({name: service})
        self.add_services_ignore()
        self.add_services_states()
        self.add_services_extra()

        self.handled_artefacts = [a for a in filter(self.artefacts_filter, self.yumdeps.requires.keys())]

        #self.handled_artefacts_with_dependencies = self.yumdeps.requires

        #self.handled_artefacts_with_dependencies = {}
        #for a in filter(self.artefacts_filter, self.yumdeps.requires.keys()):
            #self.handled_artefacts_with_dependencies[a] = self.yumdeps.requires[a]

        self.current_artefacts = self.yumdeps.requires.keys()

        self.next_artefacts = self.updates = {}
        self.yumdeps.load_all_updates()
        for a in filter(self.artefacts_filter, self.yumdeps.all_updates.keys()):
            self.updates[a] = self.yumdeps.all_updates[a]
        if len(self.next_artefacts) == 0:
            self.next_artefacts = None

        if len(self.updates):
            self.state = 'update_needed'
        else:
            self.state = 'uptodate'

        self.host = self.hostname = socket.gethostname().split('.', 1)[0]
        self.fqdn = socket.getfqdn()
        f = open('/proc/uptime', 'r')
        self.uptime = float(f.readline().split()[0])

        now = datetime.datetime.now()
        self.date = str(now)
        self.epoch = round(float(now.strftime('%s')))
        self.ip = socket.gethostbyname(socket.gethostname())

        self.interface = {}     #socket.gethostbyname_ex(socket.gethostname())[2]
        for interface in netifaces.interfaces():
            if interface == 'lo':
                continue
            self.interface[interface] = []
            try:
                for link in netifaces.ifaddresses(interface)[netifaces.AF_INET]:
                    self.interface[interface].append(link['addr'])
            except:
                pass
            self.interface[interface] = ' '.join(self.interface[interface])

        self.pwd = os.getcwd()

        self.structure_keys = [key for key in self.__dict__.keys()
            if key not in ['yumbase', 'yumdeps', 'service_defs', 'artefacts_filter']]


    @classmethod
    def load_defaults(clazz, yadt_default_filename):
        defaults = {}
        p = subprocess.Popen(
            '. %s; for thisYADT_VAR in ${!YADT_*}; do echo $thisYADT_VAR "${!thisYADT_VAR}"; done' % yadt_default_filename,
            shell=True, stdout=subprocess.PIPE)
        stdoutdata, _ = p.communicate()
        for line in stdoutdata.splitlines():
            var, value = line.split(' ', 1)
            defaults[var] = value
        return defaults

    def add_services_states(self):
        for service in [s.values()[0] for s in self.services]:
            init_script = service.get('init_script')
            if not init_script:
                continue
            cmds = [init_script, 'status']
            if self.defaults.get('YADT_YUM_COMMAND'):
                cmds = [self.defaults.get('YADT_YUM_COMMAND')] + cmds
            p = subprocess.Popen(cmds, stdout=open(os.devnull, 'w'))
            p.wait()
            service['state'] = p.returncode


    def get_lock_state(self):
        lock_file = os.path.join(self.defaults['YADT_LOCK_DIR'], 'host.lock')
        try:
            file = open(lock_file)
            return yaml.load(file)
        except IOError, e:
            if e.errno != 2:    # 2: No such file or directory
                sys.stderr.write(str(e) + '\n')
                sys.stderr.flush()
        return None


    def add_services_ignore(self):
        for service in [s.values()[0] for s in self.services]:
            ignore_file = os.path.join(self.defaults['YADT_LOCK_DIR'], 'ignore.%s' % service['name'])
            try:
                file = open(ignore_file)
                service['ignored'] = yaml.load(file)
            except IOError, e:
                if e.errno != 2:    # 2: No such file or directory
                    sys.stderr.write(str(e) + '\n')
                    sys.stderr.flush()


    def add_services_extra(self):
        executable = stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        for service in [s.values()[0] for s in self.services]:
            extra_file = '/usr/bin/yadt-status-service-%s' % service['name']
            if os.path.isfile(extra_file):
                mode = os.stat(extra_file).st_mode
                if mode & executable:
                    service['extra_script'] = extra_file
                    p = subprocess.Popen(extra_file, stdout=subprocess.PIPE)
                    stdoutdata, _ = p.communicate()
                    service['extra'] = yaml.load(stdoutdata)


    def get_status(self):
        return dict(filter(lambda item: item[0] in self.structure_keys, self.__dict__.iteritems()))