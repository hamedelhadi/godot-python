import re
from itertools import takewhile
from SCons.Script import Builder, SharedLibrary
from SCons.Util import CLVar, is_List
from SCons.Errors import UserError


### Cython to C ###


def _cython_to_c_emitter(target, source, env):
    if not source:
        source = []
    elif not is_List(source):
        source = [source]
    # Consider we always depend on all .pxd files
    source += env["CYTHON_DEPS"]

    # Add .html target if cython is in annotate mode
    if "-a" in env["CYTHONFLAGS"] or "--annotate" in env["CYTHONFLAGS"]:
        pyx = next(x for x in target if x.name.endswith(".pyx"))
        base_name = pyx.get_path().rsplit(".")[0]
        return [target[0], f"{base_name}.html"], source
    else:
        return target, source


CythonToCBuilder = Builder(
    action="cython $CYTHONFLAGS $SOURCE -o $TARGET",
    suffix=".c",
    src_suffix=".pyx",
    emitter=_cython_to_c_emitter,
)


### C compilation to .so ###


def _get_relative_path_to_libpython(env, target):
    *parts, _ = target.abspath.split("/")
    # Modules installed in `site-packages` come from `pythonscript` folder
    hops_to_site_packages = len(
        list(takewhile(lambda part: part != "pythonscript", reversed(parts)))
    )
    # Path should be `lib/python3.7/site-packages/` with `lib/libpython3.so`
    hops_to_libpython_dir = hops_to_site_packages + 2
    return "/".join([".."] * hops_to_libpython_dir)


def CythonCompile(env, target, source):
    env.Depends(source, env["cpython_build"])
    if env["platform"].startswith("windows"):
        ret = env.SharedLibrary(
            target=target,
            source=source,
            LIBPREFIX="",
            SHLIBSUFFIX="$CYTHON_SHLIBSUFFIX",
            LIBS=["python37", "pythonscript"],
        )
    else:  # x11&osx
        libpython_path = _get_relative_path_to_libpython(env, env.File(target))
        ret = env.SharedLibrary(
            target=target,
            source=source,
            LIBPREFIX="",
            SHLIBSUFFIX="$CYTHON_SHLIBSUFFIX",
            LIBS=["python3.7m", "pythonscript"],
            LINKFLAGS=[f"-Wl,-rpath,'$$ORIGIN/{libpython_path}'", *env["LINKFLAGS"]],
        )
    return ret


### Direct Cython to .so ###


def CythonModule(env, target, source=None):
    if not target:
        target = []
    elif not is_List(target):
        target = [target]

    if not source:
        source = []
    elif not is_List(source):
        source = [source]

    # mod_target is passed to the compile builder
    mod_target, *other_targets = target

    if not source:
        source.append(f"{mod_target}.pyx")

    pyx_mod, *too_much_mods = [x for x in source if str(x).endswith(".pyx")]
    if too_much_mods:
        raise UserError(
            f"Must have exactly one .pyx file in sources (got `{[mod, *too_much_mods]}`)"
        )
    c_mod = pyx_mod.split(".", 1)[0] + ".c"  # Useful to do `xxx.gen.pyx` ==> `xxx`
    CythonToCBuilder(env, target=[c_mod, *other_targets], source=source)

    c_compile_target = CythonCompile(env, target=mod_target, source=[c_mod])

    return [*c_compile_target, *other_targets]


### Scons tool hooks ###


def generate(env):
    """Add Builders and construction variables for ar to an Environment."""

    env["CYTHONFLAGS"] = CLVar("--fast-fail -3")

    # Python native module must have .pyd suffix on windows and .so on POSIX (even on macOS)
    if env["platform"].startswith("windows"):
        env["CYTHON_SHLIBSUFFIX"] = ".pyd"
    else:
        env["CYTHON_SHLIBSUFFIX"] = ".so"

    env.Append(BUILDERS={"CythonToC": CythonToCBuilder})
    env.AddMethod(CythonCompile, "CythonCompile")
    env.AddMethod(CythonModule, "CythonModule")


def exists(env):
    return env.Detect("cython")
