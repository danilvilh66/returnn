"""
Some global configurations
"""

import sys
import os
import typing
import logging

ModuleNamePrefix = "returnn_import."
logger = logging.getLogger("returnn.import_")

_DefaultPkgPath = os.path.expanduser("~/returnn/pkg")
_EnvPkgPath = "RETURNN_PKG_PATH"
_pkg_path = None  # type: typing.Optional[str]
_DefaultPkgImportPath = os.path.expanduser("~/returnn/_pkg_import")
_pkg_import_path = None  # type: typing.Optional[str]


class InvalidVersion(Exception):
  """
  The version string is invalid.
  """


def package_path():
  """
  :return: directory where packages are stored (default: ~/returnn/pkg)
  :rtype: str
  """
  global _pkg_path
  if _pkg_path:
    return _pkg_path
  if _EnvPkgPath in os.environ:
    path = os.environ[_EnvPkgPath]
    assert os.path.isdir(path), "import pkg path via env %s: is not a dir: %r" % (_EnvPkgPath, path)
  else:
    path = _DefaultPkgPath
    os.makedirs(path, exist_ok=True)
  _pkg_path = path
  return path


def _package_import_path():
  """
  :return: directory for package import
  :rtype: str
  """
  global _pkg_import_path
  if _pkg_import_path:
    return _pkg_import_path
  _pkg_import_path = _DefaultPkgImportPath
  _setup_pkg_import()
  return _pkg_import_path


def _package_import_pkg_path():
  """
  :return: directory for package import
  :rtype: str
  """
  assert ModuleNamePrefix.endswith(".")
  path = "%s/%s" % (_package_import_path(), ModuleNamePrefix[:-1])
  return path


def _setup_pkg_import():
  os.makedirs(_package_import_path(), exist_ok=True)
  _mk_py_pkg_dirs(_package_import_pkg_path())
  if _package_import_path() not in sys.path:
    sys.path.insert(0, _package_import_path())


def _mk_py_pkg_dirs(start_path, end_path=None):
  """
  :param str start_path:
  :param str|None end_path:
  """
  if end_path:
    if len(end_path) > len(start_path):
      assert end_path.startswith(start_path + "/")
    else:
      assert start_path == end_path
  else:
    end_path = start_path
  path = start_path
  while True:
    if os.path.exists(path):
      assert os.path.isdir(path) and not os.path.islink(path) and os.path.exists(path + "/__init__.py")
    else:
      os.mkdir(path)
      with open(path + "/__init__.py", "x") as f:
        f.write("# automatically generated by RETURNN\n")
        f.write("from returnn.import_.common import setup_py_pkg\n")
        f.write("setup_py_pkg(globals())\n")
        f.close()
    if len(path) == len(end_path):
      break
    p = end_path.find("/", len(path) + 1)
    if p < 0:
      path = end_path
    else:
      path = end_path[:p]


def setup_py_pkg(mod_globals):
  """
  This will get called to prepare any custom setup for the package.

  :param dict[str] mod_globals: globals() in the package
  """
  # Dummy...
  logger.debug("pkg mod %s %s", mod_globals.get("__name__"), mod_globals.get("__file__"))


def _normalize_pkg_name(name):
  """
  :param str name:
  :rtype: str
  """
  name = name.replace(".", "_")
  name = name.replace("-", "_")
  return name


def module_name(repo, repo_path, path, version):
  """
  :param str repo: e.g. "github.com/rwth-i6/returnn-experiments"
  :param str repo_path: what get_repo_path returns, e.g. "/home/az/returnn/pkg/...@v..."
  :param str path: path to file in repo
  :param str|None version: e.g. "20211231-0123abcd0123". None for development working copy
  :return: module name. as a side-effect, we make sure that importing this module works
  :rtype: str

  Note on the internals:

  We could have dynamically loaded the module directly from the package path,
  in some way.
  The reason we choose this different approach to create a real directory
  with symlinks is such that we can potentially make use
  of auto-completion features in editors.
  It might also make debugging easier.
  So, in this function, we make sure that all symlinks are correctly setup.
  """
  full_path = "%s/%s" % (repo_path, path)
  py_pkg_dirname = _find_root_python_package(full_path)
  assert len(py_pkg_dirname) >= len(repo_path)
  rel_pkg_path = full_path[len(py_pkg_dirname) + 1:]
  p = rel_pkg_path.find("/")
  if p > 0:
    rel_pkg_path0 = rel_pkg_path[:p]
  else:
    rel_pkg_path0 = rel_pkg_path
  rel_pkg_dir = py_pkg_dirname[len(repo_path):]  # starting with "/"

  repo_dir_name = os.path.dirname(repo)
  repo_v = "%s/%s" % (repo_dir_name, os.path.basename(repo_path))  # eg "github.com/rwth-i6/returnn-experiments@v..."
  if version:
    repo_v = repo_v.replace("@v", "/v")
  else:
    repo_v = repo_v + "/dev"
  py_pkg_dir = "%s/%s%s" % (_package_import_pkg_path(), _normalize_pkg_name(repo_v), _normalize_pkg_name(rel_pkg_dir))
  _mk_py_pkg_dirs(_package_import_pkg_path(), py_pkg_dir)
  symlink_file, target_file_extension = os.path.splitext("%s/%s" % (py_pkg_dir, rel_pkg_path0))
  symlink_target = "%s%s/%s" % (repo_path, rel_pkg_dir, rel_pkg_path0)
  if target_file_extension:
    # if the target has a file extension, force it to be always .py for the symlink
    symlink_file = symlink_file + ".py"
  if os.path.exists(symlink_file):
    assert os.readlink(symlink_file) == symlink_target
  else:
    logger.debug("Symlink %s -> %s", symlink_file, symlink_target)
    os.symlink(symlink_target, symlink_file, target_is_directory=os.path.isdir(symlink_target))
  repo_and_path = "%s/%s" % (repo_v, os.path.splitext(path)[0])
  name = _normalize_pkg_name(repo_and_path).replace("/", ".")
  return ModuleNamePrefix + name


def _find_root_python_package(full_path):
  """
  :param str full_path: some Python file
  :return: going up from path, and first dir which does not include __init__.py
  :rtype: str
  """
  p = len(full_path)
  while True:
    p = full_path.rfind("/", 0, p)
    assert p > 0
    d = full_path[:p]
    if not os.path.exists(d + "/__init__.py"):
      return d
