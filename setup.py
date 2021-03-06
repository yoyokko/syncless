#! /usr/local/bin/stackless2.6
# by pts@fazekas.hu at Tue Feb  9 18:42:57 CET 2010

"""Python distutils setup.py build script for Syncless.

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""

__author__ = 'pts@fazekas.hu (Peter Szabo)'

def IsGoodGreenlet(greenlet):
  return bool(
      getattr(getattr(greenlet, 'greenlet', None), 'throw', None) and
      getattr(greenlet, '__version__', '') >= '0.3.1')

import sys
# We don't try to `import stackless' here delibaretely, because we are not
# interested in emulations, but we're interested in the `stackless' module
# available by default in Stackless Python.
have_stackless = 'stackless' in sys.modules
try:
  import greenlet
  have_greenlet = True
  have_good_greenlet = IsGoodGreenlet(greenlet)
except ImportError:
  have_greenlet = have_good_greenlet = False

import glob
import os
import os.path
import re
import sys
import stat

install_requires = []
if 'bdist_egg' in sys.argv:
  # This is when we're executed by easy_install.
  # Example sys.argv: ['.../setup.py', '-q', 'bdist_egg', '--dist-dir', '.../egg-dist-tmp-AEgKbo']
  import setuptools
  # We don't download anything if setuptools is loaded (i.e. setup.py is
  # started by easy_install), because install_requires=[...] below will take
  # care of installing greenlet.
  if not have_stackless and not have_good_greenlet:
    have_greenlet = have_good_greenlet = True
    install_requires.append('greenlet>=0.3.1')

from distutils import log
from distutils.ccompiler import CCompiler
from distutils.ccompiler import new_compiler
from distutils.command.build import build
from distutils.command.build_ext import build_ext
from distutils.core import Command
from distutils.core import Extension
from distutils.core import setup
from distutils.dir_util import mkpath
from distutils.dist import Distribution
from distutils.errors import CompileError
from distutils.errors import DistutilsError
from distutils.errors import LinkError
from distutils.unixccompiler import UnixCCompiler

UnixCCompiler.nosyncless_find_library_file = UnixCCompiler.find_library_file

def FindLibraryFile(self, dirs, lib, *args, **kwargs):
  if len(dirs) == 1 and os.path.isfile(os.path.join(dirs[0], lib)):
    # e.g. '/usr/local/lib/libfoo.so.5'
    return os.path.join(dirs[0], lib)
  return self.nosyncless_find_library_file(dirs, lib, *args, **kwargs)

UnixCCompiler.find_library_file = classmethod(FindLibraryFile)

class MyBuild(build):
  # Non-standard, extra method.
  def has_sources(self):
    return self.has_pure_modules or self.has_ext_modules

  sub_commands = [
      ('build_install_greenlet', lambda *args: True),
      ] + build.sub_commands + [
      ('build_ext_symlinks', build.has_ext_modules),
      ('build_src_symlinks', has_sources)]

class MyBuildExt(build_ext):
  def run(self):
    if self.extensions:
      self.run_command('build_ext_dirs')
      build_ext.run(self)

def GetUrlAndUserFromSvn(svn_client_dir):
  """Return (svn_url, svn_user)."""
  # This has been tested with svn 1.4.6.
  f = open(os.path.join(svn_client_dir, '.svn', 'entries'))
  lines = f.read().replace('\r\n', '\n').split('\n')
  assert '://' in lines[5]
  return lines[5], lines[11]

def GetSvnPassword(svn_url, svn_user):
  """Return password string stored in ~/.subversion."""
  # This has been tested with svn 1.4.6.
  dir_name = os.path.join(
      os.getenv('HOME', '/dev/null'), '.subversion', 'auth', 'svn.simple')
  if not os.path.isdir(dir_name):
    raise DistutilsError('svn config dir not found: %s' % dir_name)
  svn_url_slash = svn_url.rstrip('/') + '/'
  for entry in sorted(os.listdir(dir_name)):
    file_name = os.path.join(dir_name, entry)
    f = open(file_name)
    # Example f.read():
    # 'K 8\npassword\nV 6\nsecret\nK 8\nusername\nV 3\nmyuser\nEND'.
    h = {}
    try:
      while True:
        line = f.readline()
        if line in ('', 'END', 'END\n'):
          break
        match = re.match(r'K\s+(\d+)\n\Z', line)
        assert match, 'expected key as K'
        size = int(match.group(1))
        key = f.read(size)
        assert len(key) == size
        line = f.read(1)
        assert line == '\n'

        line = f.readline()
        match = re.match(r'V\s+(\d+)\n\Z', line)
        assert match, 'expected value as V'
        size = int(match.group(1))
        value = f.read(size)
        assert len(value) == size
        line = f.read(1)
        assert line == '\n'
        h[key] = value
    finally:
      f.close()
    if h.get('username') != svn_user:
      continue
    if not h.get('password'):
      continue
    realm = h.get('svn:realmstring', '')
    if not realm.startswith('<') or '>' not in realm:
      continue
    realm = realm[1 : realm.find('>')] + '/'
    # Example realm: 'https://syncless.googlecode.com:443/'.
    if '://' not in realm:
      continue
    if realm.startswith('https://'):
      realm = realm.replace(':443/', '/')
    if svn_url_slash.startswith(realm):
      return h['password']
  raise DistutilsError('password not found for SVN user %s and URL %s' %
                       (svn_user, svn_url))

class MyUpload(Command):
  """Upload source .tar.gz distributions to Google Code.

  This class is based on
  http://support.googlecode.com/svn/trunk/scripts/googlecode_distutils_upload.py
  """

  labels = ['Featured', 'Type-Source', 'OpSys-Linux', 'OpSys-All']

  description = 'upload source or Windows distribution to Google Code'
  user_options = [
                  ('dist-dir=', 'd',
                   'directory to find distribution archive in'
                   ' [default: dist]'),
                  ('user=', 'u',
                   'Google Code username'),
                  ('password=', 'p',
                   'Google Code password'),
                  ]

  def initialize_options(self):
    self.dist_dir = None
    self.user = None
    self.password = None

  def finalize_options(self):
    # Get dist-dir default from sdist or bdist_wininst.
    self.set_undefined_options('sdist', ('dist_dir', 'dist_dir'))

    # Do nothing for config-dir and user; upload_find_auth does the
    # right thing when they're None.

  def run(self):
    # TODO(pts): Make the old file not featured. (This is not possible yet
    # since there is no Google Code Data API yet.)
    import googlecode_upload
    name = self.distribution.get_name()
    version = self.distribution.get_version()

    # TODO(epg): sdist is more flexible with formats...
    fn = os.path.join(self.dist_dir, self.distribution.get_fullname())
    fn += '.tar.gz'
    if not os.path.isfile(fn):
      raise DistutilsError('missing source distribution file %s, '
                           'run setup.py sdist' % fn)
    summary = ' '.join([name, version, 'source distribution'])
    labels = self.labels

    if self.user is None or self.password is None:
      svn_url, user = GetUrlAndUserFromSvn('.')
      if self.user is not None and user != self.user:
        raise DistutilsError('found svn user %r, got cmdline user %r' %
                             (user, self.user))
      if self.password is None:
        password = GetSvnPassword(svn_url, user)
      else:
        password = self.password
    else:
      user = self.user
      password = self.password

    (status, reason,
     file_url) = googlecode_upload.upload_find_auth(
         file_path=fn, project_name=name, summary=summary, labels=labels,
         user_name=user, password=password)
    if file_url is None:
      raise DistutilsError('upload error: %s (%d)\n' % (reason, status))

    sys.stdout.write('Uploaded %s\n' % (file_url,))

# --- Simple unit test framework

import logging
import os
import os.path
import sys
import traceback
import time

def is_logging_to_tty():
  """Return true iff all logging destinations are terminal devices."""
  if not logging.root.handlers:
    logging.basicConfig()
  assert logging.root.handlers
  for handler in logging.root.handlers:
    if not (isinstance(handler, logging.StreamHandler) and
            handler.stream.isatty()):
      return False
  return True

def bold(string, is_tty):
  if not is_tty:
    return string
  return '\033[0;1m%s\033[0m' % string

def red(string, is_tty):
  if not is_tty:
    return string
  return '\033[0;1;31m%s\033[0m' % string

def green(string, is_tty):
  if not is_tty:
    return string
  return '\033[0;1;32m%s\033[0m' % string

def run_test(test_file, is_tty):
  logging.info('running test: ' + bold(test_file, is_tty))

  sys.stdout.flush()
  sys.stderr.flush()
  # We use fork to make sure we run all tests with the same interpreter and
  # same settings.
  #
  # TODO(pts): Add support for platforms without fork (like call
  # sys.executable).
  pid = os.fork()

  if not pid:  # Child.
    exit_code = 125

    # Make sure we don't use imported symbols below.
    exit = os._exit
    print_exc = traceback.print_exc

    try:
      try:
        del sys.argv[1:]
        sys.path[:0] = [os.path.dirname(test_file) or '.']
        import __main__
        import __builtin__
        # TODO(pts): Unload all modules from sys.modules.
        __main__.__dict__.clear()
        __main__.__builtin__ = __builtin__
        __main__.__name__ = '__main__'
        exec open(test_file) in __main__.__dict__
        # This is not always reached (but SystemExit below may be).
        exit_code = 0
      except SystemExit, e:
        exit_code = e[0]
      except:
        exit_code = 126
        print_exc()
    finally:
      exit(exit_code)

  # TODO(pts): Add timeout.
  pid2, status = os.waitpid(pid, 0)
  assert pid2 == pid, (pid2, pid)
  if status:
    logging.info('test %s %s, exit status: 0x%x' %
                 (test_file, red('failed', is_tty), status))
  else:
    logging.info('test %s %s' % (test_file, green('passed', is_tty)))
  sys.stdout.flush()
  sys.stderr.flush()
  return not status

def run_tests(test_dir, is_tty):
  test_files = sorted(
      os.path.join(test_dir, entry) for entry in os.listdir(test_dir)
      if entry.endswith('_test.py') and not entry.startswith('.'))
  tests_failed = []
  start_at = time.time()
  for test_file in test_files:
    if not run_test(test_file, is_tty):
      tests_failed.append(test_file)
  duration = time.time() - start_at
  if tests_failed:
    logging.info('tests failed: ' + ' '.join(tests_failed))
    logging.info('%s running tests with %s, %d %s out of %d' %
                 (red('done', is_tty), sys.executable, len(tests_failed),
                  red('failure' + 's' * (len(tests_failed) > 1), is_tty),
                  len(test_files)))
    return False
  else:
    logging.info('%s running tests with %s, all %d %s in %.3f seconds' %
                 (green('done', is_tty), sys.executable, len(test_files),
                  green('passed', is_tty), duration))
    return True

# --- End of the test framework.

class MyTest(Command):
  """Run unit tests."""

  user_options = []  # Required by setuptools.

  def run(self):
    logging.BASIC_FORMAT = '[%(created)f] %(levelname)s %(message)s'
    logging.root.setLevel(logging.DEBUG)
    logging.info('running tests under interpreter: ' + sys.executable)
    # Do the test after logging at least 1 line, for safety.
    is_tty = is_logging_to_tty()
    if not run_tests(test_dir='test', is_tty=is_tty):
      raise DistutilsError('some tests failed')

  def initialize_options(self):
    pass

  def finalize_options(self):
    pass


class MyBuildInstallGreenlet(Command):
  """Command to install greenlet, a non-.egg version."""

  def run(self):
    global have_greenlet
    global have_good_greenlet
    if not have_stackless and not have_good_greenlet:
      if have_greenlet:
        log.info('no stackless found, but found old (unusable) greenlet')
      else:
        log.info('no stackless or greenlet found')
      log.info('attempting to download and compile greenlet with easy_install')
      install_purelib = self.get_finalized_command(
          'install').install_purelib
      # Not created yet:
      #so_ext = command_obj.get_finalized_command(
      #    'build_ext').compiler.shared_lib_extension
      from distutils.ccompiler import new_compiler
      so_ext = new_compiler().shared_lib_extension  # '.so'
      from distutils.file_util import copy_file
      from distutils.dir_util import remove_tree
      from distutils.dir_util import copy_tree

      # TODO(pts): Auto-detect pip (is it calling us, setup.py?) and try pip.
      try:
        from setuptools.command import easy_install
      except ImportError:
        easy_install = None
      if not easy_install:
        raise LinkError('neither stackless or greenlet found, '
                        'and easy_install was not found either to install them, '
                        'see the Installation section of README.txt')


      old_log_level = log._global_log.threshold
      mkpath('tmp')
      greenlet_file_pattern =  'tmp/greenlet-*.egg'
      for filename in glob.glob(greenlet_file_pattern):
        remove_tree(filename)
      if easy_install.main(['-dtmp', '-m', '-U', '-Z', 'greenlet>=0.3.1']):
        # We usually won't reach this, easy_install calls sys.exit.
        raise DistutilsError('installation of greenlet failed')

      # Restore values overridden by easy_install.main.
      # See also setuptools.dist for overriding Distribution.
      log.set_threshold(old_log_level)
      for module in ('distutils.dist', 'distutils.core', 'distutils.cmd'):
        __import__(module, {}, {}, ('',)).Distribution = Distribution
      for module in ('distutils.core', 'distutils.extension',
                     'distutils.command.build_ext'):
        __import__(module, {}, {}, ('',)).Extension = Extension

      # Now extract greenlet.so from tmp/greenlet-*.egg
      log.info('extracting greenlet from its egg and installing it')
      greenlet_file_names = glob.glob(greenlet_file_pattern)
      if (len(greenlet_file_names) != 1 or
          not os.path.isdir(greenlet_file_names[0])):
        raise DistutilsError('could not find any of: %s' % greenlet_file_pattern)
      greenlet_so = 'greenlet' + so_ext
      copy_file(os.path.join(greenlet_file_names[0], greenlet_so),
                install_purelib)
      egg_info_dir = (os.path.join(
          install_purelib,
          '-'.join(os.path.basename(greenlet_file_names[0]).split('-')[:-2])) +
          '.egg-info')
      if os.path.isfile(os.path.join(egg_info_dir, 'PKG-INFO')):
        remove_tree(egg_info_dir)
      copy_tree(os.path.join(greenlet_file_names[0], 'EGG-INFO'), egg_info_dir)

      sys.modules.pop('greenlet', None)
      try:
        import greenlet
        have_greenlet = True
        have_good_greenlet = IsGoodGreenlet(greenlet)
      except ImportError:
        raise LinkError('neither stackless or greenlet found, '
                        'and could not import greenlet after easy_install, '
                        'see the Installation section of README.txt')

  def initialize_options(self):
    pass

  def finalize_options(self):
    pass


class MyBuildExtDirs(Command):
  """Autodetect the C extension compiler environment. """

  user_options = []  # Required by setuptools.

  def run(self):
    command_obj = self
    for ext in command_obj.distribution.ext_modules:
      #assert ext.include_dirs == []
      if callable(ext.library_dirs):
        update_dict = ext.library_dirs(command_obj)
        assert isinstance(update_dict, dict)
        ext.library_dirs = []
        ext.include_dirs = []
        for key in sorted(update_dict):
          if key == 'python_version':
            continue
          value = update_dict[key]
          assert isinstance(value, list) or isinstance(value, tuple)
          assert hasattr(ext, key), key
          assert isinstance(getattr(ext, key), list)
          # ext.library_dirs.extend(update_dict['library_dirs'])
          getattr(ext, key).extend(value)

  def initialize_options(self):
    pass

  def finalize_options(self):
    pass


class MyBuildExtSymlinks(Command):
  """Create symlinks to .so files.
  
  Create symlinks so scripts in the source dir can be run with PYTHONPATH=.
  without install.
  """

  def run(self):
    # '.so'
    build_ext_cmd = self.get_finalized_command('build_ext')
    so_ext = build_ext_cmd.compiler.shared_lib_extension
    for ext in self.distribution.ext_modules:
      name_items = ext.name.split('.')
      if len(name_items) == 2 and os.path.isdir(name_items[0]):
        # build_ext_cmd.build_lib: 'build/lib.linux-i686-2.6'
        # '.'.join(name_items) == 'syncless/coio'
        so_file = os.path.join(build_ext_cmd.build_lib,
                               name_items[0], name_items[1] + so_ext)
        link_from = os.path.join(name_items[0], name_items[1] + so_ext)
        link_to = os.path.join('..', so_file)
        symlink(link_to, link_from)

  def initialize_options(self):
    pass

  def finalize_options(self):
    pass


class MyBuildSrcSymlinks(Command):
  """Create symlinks to package source directories.
  
  Create symlinks so, if combined with MyBuildExtSymlinks, scripts in the
  source dir can be run even without PYTHONPATH=. without install.
  """

  def run(self):
    src_dirs = self.distribution.symlink_script_src_dirs
    if src_dirs:
      for package in self.distribution.packages:  # package = 'syncless'
        package = package.replace('/', '.')
        if '.' not in package:
          for src_dir in src_dirs:  # src_dir = 'test'
            link_from = os.path.join(src_dir, package)
            link_to = os.path.join('..', package)
            symlink(link_to, link_from)

  def initialize_options(self):
    pass

  def finalize_options(self):
    pass

# Make self.distribution.symlink_script_src_dirs visible in Command objects.
Distribution.symlink_script_src_dirs = None
Distribution.install_requires = None
Distribution.zip_safe = None

def symlink(link_to, link_from):
  log.info('symlinking %s -> %s' % (link_from, link_to))
  try:
    st = os.lstat(link_from)
    if stat.S_ISLNK(st.st_mode):
      os.remove(link_from)  # Remove old symlink.
  except OSError:
    pass
  os.symlink(link_to, link_from)


def GetCompiler(command_obj):
  build_ext_cmd = command_obj.get_finalized_command('build_ext')
  if build_ext_cmd.dry_run:
    return retval  # TODO(pts): Do better.
  if isinstance(build_ext_cmd.compiler, CCompiler):
    return build_ext_cmd.compier
  else:
    return new_compiler(compiler=build_ext_cmd.compiler,
                        verbose=build_ext_cmd.verbose,
                        dry_run=build_ext_cmd.dry_run,
                        force=build_ext_cmd.force)


def HasSymbols(compiler, symbols=(),
               includes=None,
               include_dirs=None,
               libraries=None,
               library_dirs=None):
  """Return a boolean indicating whether all symbols are defined on
  the current platform.  The optional arguments can be used to
  augment the compilation environment.
  
  Different from CCompiler.has_function:

  * ignores function arguments, just tries to convert to (void*).
  * multiple symbols (conjunction)
  * symbols should not be macros (so library linking can be tested)
  * runs the linked executable with /bin/sh
  """
  if not symbols:
    return True

  import tempfile
  if includes is None:
    includes = []
  if include_dirs is None:
    include_dirs = []
  if libraries is None:
    libraries = []
  if library_dirs is None:
    library_dirs = []
  mkpath('tmp')
  fname = 'tmp/check_c_sym_' + symbols[0] + '.c'
  f = open(fname, "w")
  if () in includes:
    assert len(includes) == 1
    for symbol in symbols:
      f.write("extern void %s(void);\n" % symbol)
  else:
    for incl in includes:
      f.write("""#include "%s"\n""" % incl)
  f.write("int main(int argc, char **argv) {\n")
  f.write("  int c = %d;\n" % len(symbols))
  for symbol in symbols:
    f.write("  if ((void*)(%s) != (void*)main) --c;\n" % symbol)
  f.write("  return c;\n");
  f.write("}\n")
  f.close()
  try:
    # Strips leading '/' from fname.
    objects = compiler.compile([fname], include_dirs=include_dirs)
  except CompileError:
    return False

  execfn = os.path.splitext(fname)[0] + '.out'
  try:
    compiler.link_executable(objects, execfn,
                             libraries=libraries,
                             library_dirs=library_dirs)
  except (LinkError, TypeError):
    return False
  assert '/' in execfn
  assert '\0' not in execfn
  if 0 != os.system("exec '%s'" % execfn.replace("'", "'\\''")):
    log.error('running %s failed' % execfn)
    return False
  return True


def FindLib(retval, compiler, prefixes, includes, library, symbols,
            link_with_prev_libraries=None):
  for prefix in prefixes:
    if (prefix and
        (() in includes or includes ==
         [include for include in includes if
          include.startswith('./') or
          os.path.isfile(prefix + '/include/' + include)]) and
        ('/' in library or 
         glob.glob(prefix + '/lib/lib' + library + '.*'))):
      include_dir = '%s/include' % prefix
      library_dir = '%s/lib' % prefix
      libraries = [library]
      if link_with_prev_libraries:
        # library_dirs same order as in final retval.
        library_dirs = list(retval['library_dirs'])
        libraries.extend(link_with_prev_libraries)
      else:
        library_dirs = []
      if library_dir != os.path.dirname(library):
        library_dirs.append(library_dir)
      include_dirs = [include_dir]
      if [1 for include in includes if include is not () and
          include.startswith('./')]:
        include_dirs[:0] = ['.']
      if HasSymbols(compiler=compiler, symbols=symbols,
                    includes=includes, include_dirs=include_dirs,
                    libraries=libraries, library_dirs=[library_dir]):
        if '/' in library:
          log.info('found %s as %s' % (os.path.basename(library), library))
        else:
          log.info('found lib%s in %s' % (library, prefix))
        if include_dir not in retval['include_dirs']:
          retval['include_dirs'].append(include_dir)
        if (library_dir != os.path.dirname(library) and
            library_dir not in retval['library_dirs']):
          retval['library_dirs'].append(library_dir)
        retval['is_found'] = True
        return True
  log.error('library %s not found or not working' % library)
  return False

def AutoDetect(command_obj):
  """Auto detect compilation information for a C extension.

  Args:
    command_obj: Any distutils.command object.
  """
  # We could add more directories (e.g. those in /etc/ld.so.conf), but that's
  # system-specific, see http://stackoverflow.com/questions/2230467 .
  # TODO(pts): Issue a fatal error if libev or libevhdns was not found.
  # TODO(pts): Find libevhdns separately.
  retval = {'include_dirs': [], 'library_dirs': [], 'libraries': [],
            'define_macros': [], 'is_found': False, 'sources': [],
            'depends': []}

  if have_stackless:
    retval['define_macros'].append(('COIO_USE_CO_STACKLESS', None))
  elif have_greenlet:
    if not have_good_greenlet:
      raise LinkError('detected old (unusable) version of greenlet, '
                      'see the Installation section of README.txt')
    retval['define_macros'].append(('COIO_USE_CO_GREENLET', None))
  else:
    raise LinkError('neither stackless or greenlet found, '
                    'see the Installation section of README.txt')

  compiler = GetCompiler(command_obj)
  prefixes = filter(bool, os.getenv('LD_LIBRARY_PATH', '').split(':') +
                    [sys.prefix, '/usr/local', '/usr'])
  is_found = False

  is_asked = False
  event_driver = None
  if os.getenv('SYNCLESS_USE_LIBEV', ''):
    assert event_driver is None
    event_driver = 'libev'
  if os.getenv('SYNCLESS_USE_LIBEVENT1', ''):
    assert event_driver is None
    event_driver = 'libevent1'
  if os.getenv('SYNCLESS_USE_LIBEVENT2', ''):
    assert event_driver is None
    event_driver = 'libevent2'
  if os.getenv('SYNCLESS_USE_MINIEVENT', ''):
    assert event_driver is None
    event_driver = 'minievent'
  asked_for_libevhdns = bool(os.getenv('SYNCLESS_USE_LIBEVHDNS', ''))
  allowed_minievent = bool(os.getenv('SYNCLESS_ALLOW_MINIEVENT', '1'))

  if event_driver in (None, 'libev'):
    if FindLib(retval=retval, compiler=compiler, prefixes=prefixes,
               includes=['ev.h'], library='ev', symbols=['ev_once']):
      if asked_for_libevhdns:
        if not FindLib(retval=retval, compiler=compiler, prefixes=prefixes,
            includes=[()], library='evhdns', symbols=['evdns_resolve_ipv4'],
            link_with_prev_libraries=['ev']):
          raise LinkError('evhdns not found')
        retval['define_macros'].append(('COIO_USE_LIBEVHDNS', None))
        retval['libraries'].append('evhdns')
      else:
        retval['define_macros'].append(('COIO_USE_MINIHDNS', None))
        retval['sources'].append('coio_src/coio_minihdns.c')
      event_driver = 'libev'
      retval['is_found'] = True
      retval['libraries'].append('ev')
      retval['define_macros'].append(('COIO_USE_LIBEV', None))

  # TODO(pts): Add support for evhdns here.
  if event_driver in (None, 'libevent2'):
    if asked_for_libevhdns:
      # TODO(pts): Add support for evhdns here.
      raise LinkError('evhdns does not work yet with libevent2')
    # event2/event_compat.h in libevent-2.0.10 doesn't contain a prototype
    # for event_reinit() and event_add() (they are in event2/event.h), so we
    # don't check for those here.
    if (FindLib(retval=retval, compiler=compiler, prefixes=prefixes,
            includes=['event2/event_compat.h'], library='event',
            symbols=['event_init', 'event_loop']) and
        FindLib(retval=retval, compiler=compiler, prefixes=prefixes,
            includes=['event2/dns.h', 'event2/dns_compat.h'],
            library='event',
            symbols=['evdns_resolve_ipv4', 'evdns_resolve_reverse_ipv6'])):
      event_driver = 'libevent2'
      retval['is_found'] = True
      # TODO(pts): Try to link something libevent1 doesn't have.
      retval['libraries'].extend(['event'])
      retval['define_macros'].append(('COIO_USE_LIBEVENT2', None))

  if event_driver in (None, 'libevent1'):
    if asked_for_libevhdns:
      # TODO(pts): Add support for evhdns here.
      raise LinkError('evhdns does not work yet with libevent1')
    lib_event = 'event'
    prefixes2 = list(prefixes)
    for prefix in prefixes:
      # Prefer libevent-1.4.so* because libevent.so might be libevent2 (sigh).
      lib_file = os.path.join(prefix, 'lib', 'libevent-1.4.so.2')
      if os.path.isfile(lib_file):
        lib_event = lib_file
        prefixes2 = [prefix]
        break
    if (FindLib(retval=retval, compiler=compiler, prefixes=prefixes2,
            includes=['./coio_src/coio_event1_event.h'], library=lib_event,
            symbols=['event_init', 'event_loop', 'event_reinit']) and
        FindLib(retval=retval, compiler=compiler, prefixes=prefixes2,
            includes=['evdns.h'], library=lib_event,
            symbols=['evdns_resolve_ipv4', 'evdns_resolve_reverse_ipv6'])):
      event_driver = 'libevent1'
      retval['is_found'] = True
      retval['libraries'].append(lib_event)
      retval['define_macros'].append(('COIO_USE_LIBEVENT1', None))

  if event_driver in ('minievent', None) and allowed_minievent:
    event_driver = 'minievent'
    retval['is_found'] = True
    retval['define_macros'].append(('COIO_USE_MINIEVENT', None))
    if asked_for_libevhdns:
      if not FindLib(retval=retval, compiler=compiler, prefixes=prefixes,
          includes=[()], library='evhdns', symbols=['evdns_resolve_ipv4'],
          link_with_prev_libraries=['ev']):
        raise LinkError('evhdns not found')
      retval['define_macros'].append(('COIO_USE_LIBEVHDNS', None))
      retval['libraries'].append('evhdns')
    else:
      retval['define_macros'].append(('COIO_USE_MINIHDNS', None))
      retval['sources'].append('coio_src/coio_minihdns.c')
    retval['sources'].append('coio_src/coio_minievent.c')
    retval['depends'].append('coio_src/coio_minievent.h')

  if not retval.pop('is_found'):
    raise LinkError('libevent/libev not found, '
                    'see the Installation section of README.txt')

  retval['python_version'] = sys.version
  repr_retval = repr(retval)
  if 'setuptools' in sys.modules and '-q' in sys.argv:
    log.error('using C env %s' % repr_retval)
  else:
    log.info('using C env %s' % repr_retval)
  try:
    old_repr_retval = open('setup.cenv').read()
  except IOError:
    old_repr_retval = None
  if repr_retval != old_repr_retval:
    open('setup.cenv', 'w').write(repr_retval)
  return retval


event = Extension(name='syncless.coio',
                  sources=['coio_src/coio.c'],
                  depends=['coio_src/coio_c_helper.h',
                           'coio_src/coio_c_evbuffer.h',
                           'coio_src/coio_c_fastsearch.h',
                           'coio_src/coio_c_stackless.h',
                           'coio_src/coio_c_include_libevent.h',
                           'coio_src/coio_ev_event.h',
                           'coio_src/coio_event1_event.h',
                           'coio_src/coio_minievent.h',
                           'setup.cenv',
                          ],
                  # Using a function for library_dirs here is a nonstandard
                  # distutils extension, see also MyBuildExtDirs.
                  library_dirs=AutoDetect,
                  libraries=[])

# chdir to to the directory containing setup.py. Building extensions wouldn't
# work otherwise.
os.chdir(os.path.join('.', os.path.dirname(__file__)))
if __file__[0] != '/':
    __file__ = os.path.basename(__file__)

os.umask(0022)

version = {}
f = open(os.path.join('syncless', 'version.py'))
exec f in version
assert isinstance(version.get('VERSION'), str)

setup(name='syncless',
      version=version['VERSION'],
      description='Syncless: asynchronous client and server library using Stackless Python',
      author='Peter Szabo',
      author_email='pts@fazekas.hu',
      maintainer='Peter Szabo',
      maintainer_email='pts@fazekas.hu',
      url='http://code.google.com/p/syncless/',
      download_url='http://syncless.googlecode.com/files/syncless-%s.tar.gz' %
                   version['VERSION'],
      packages=['syncless'],
      long_description=
          "Syncless is a non-blocking (asynchronous) concurrent client and server "
          "socket network communication library for Stackless Python 2.6 (and also for "
          "regular Python with greenlet). For high speed, Syncless uses libev (and "
          "libevent) for event notification, and parts of Syncless' code is implemented "
          "in Pyrex/Cython and C. This alone makes Syncless faster than many of its "
          "non-blocking network libraries for Python. Syncless contains an asynchronous "
          "DNS resolver (using evdns) and a HTTP server capable of serving WSGI "
          "applications. Syncless aims to be a coroutine-based alternative of "
          "event-driven networking engines (such as Twisted, asyncore, pyevent, "
          "python-libevent and FriendFeed's Tornado), and it's a competitor of gevent, "
          "Eventlet and Concurrence.",
      license="Apache License, Version 2.0",
      platforms=["Unix"],
      classifiers=[
          "Development Status :: 3 - Alpha",
          "Environment :: Console",
          "Environment :: No Input/Output (Daemon)",
          "Environment :: Other Environment",
          "Intended Audience :: Developers",
          "License :: OSI Approved :: Apache Software License",
          "Operating System :: POSIX :: Linux",
          "Operating System :: Unix",
          "Programming Language :: Python :: 2.5",
          "Programming Language :: Python :: 2.6",
          "Topic :: Internet",
          "Topic :: Internet :: WWW/HTTP :: Dynamic Content :: CGI Tools/Libraries",
          "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
          "Topic :: Internet :: WWW/HTTP :: WSGI :: Application",
          "Topic :: Internet :: WWW/HTTP :: WSGI :: Server",
          "Topic :: Software Development :: Libraries :: Application Frameworks",
          "Topic :: Software Development :: Libraries :: Python Modules",
      ],
      # Only one of stackless or greenlet is required, but it's impossible to
      # specify that here.
      requires=['stackless', 'greenlet (>=0.3.1)'],
      install_requires=[install_requires],  # for setuptools
      zip_safe=True,  # for setuptools
      ext_modules=[event],
      cmdclass = {'build': MyBuild,
                  'build_install_greenlet': MyBuildInstallGreenlet,
                  'build_ext_dirs': MyBuildExtDirs,
                  'build_ext': MyBuildExt,
                  'build_ext_symlinks': MyBuildExtSymlinks,
                  'build_src_symlinks': MyBuildSrcSymlinks,
                  'upload': MyUpload,
                  'test': MyTest,
                 },
      # Mentioning `syncless' here would cause double loading of syncless.coio from
      # examples/demo.py.
      symlink_script_src_dirs=['test', 'benchmark', 'coio_src', 'examples'],
     )
