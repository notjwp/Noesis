"""Install NOESIS from a git checkout, and say where the command landed.

One line from anywhere, no `curl | sh` - the policy gate in agent/policy.py
classifies that shape as destructive, and an installer this project ships
should be one it would run:

  macOS, Linux   git clone https://github.com/notjwp/Noesis.git && cd Noesis && python3 scripts/install.py
  Windows        git clone https://github.com/notjwp/Noesis.git; cd Noesis; python scripts/install.py

Python and not sh: it is the one interpreter all three have, and the same
file has to do the same thing on each. `python` on Windows and `python3` on
the others because a Windows virtualenv has no python3.exe - `python3` there
is the Microsoft Store alias, and installs into a Python nobody chose.

A CHECKOUT, not a package: `noesis --update` is `git pull` in this tree, so a
`pip install git+...` would install something that cannot update itself.
Editable, so the pull IS the update. Idempotent: run it again after a pull
and pip reports the requirements already satisfied.
"""
import os
import shutil
import subprocess
import sys
import sysconfig

REPO = "https://github.com/notjwp/Noesis.git"


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def venv_python():
    """The active virtualenv's own interpreter, by path, or None."""
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        return None
    for candidate in (os.path.join(venv, "bin", "python"),
                      os.path.join(venv, "Scripts", "python.exe")):
        if os.path.isfile(candidate):
            return candidate
    fail("VIRTUAL_ENV is set to %s but no python was found inside it." % venv)


def main():
    # A venv is active but this is not its python: hand over to the one that
    # owns the venv, so the install lands where the person put themselves.
    venv = venv_python()
    if venv and os.path.realpath(venv) != os.path.realpath(sys.executable):
        sys.exit(subprocess.call([venv, os.path.abspath(__file__)] + sys.argv[1:]))
    if sys.version_info < (3, 12):
        fail("NOESIS needs Python 3.12 or newer; %s is %s."
             % (sys.executable, sys.version.split()[0]))
    if not shutil.which("git"):
        fail("NOESIS needs git on PATH: the install is a checkout and updates are a pull.")

    # Inside the checkout already, or clone one here. `name = "noesis"` is the
    # line that says this pyproject is ours and not some other project's.
    cwd = os.getcwd()
    root = None
    for candidate in (cwd, os.path.join(cwd, "Noesis")):
        pyproject = os.path.join(candidate, "pyproject.toml")
        if os.path.isfile(pyproject):
            with open(pyproject, encoding="utf-8") as handle:
                if 'name = "noesis"' in handle.read():
                    root = candidate
                    break
    if root is None:
        subprocess.run(["git", "clone", REPO, "Noesis"], check=True)
        root = os.path.join(cwd, "Noesis")
    print("installing from %s with %s" % (root, sys.executable), flush=True)

    # Into the active virtualenv if there is one, else the user site. Never
    # the system site-packages: that needs root and breaks the distro's Python.
    pip = [sys.executable, "-m", "pip", "install"] + ([] if venv else ["--user"]) + ["-e", "."]
    done = subprocess.run(pip, cwd=root)
    if done.returncode:
        sys.exit(done.returncode)
    scripts = os.path.realpath(
        sysconfig.get_path("scripts") if venv else sysconfig.get_path("scripts", os.name + "_user"))

    # The command THIS install wrote, and whether PATH reaches it - it must be
    # this install's `noesis`, not a stale one somewhere else on PATH.
    written = next((f for f in ("noesis", "noesis.exe")
                    if os.path.isfile(os.path.join(scripts, f))), None)
    found = shutil.which("noesis")
    print()
    if written is None:
        fail("pip finished but wrote no noesis command into %s." % scripts)
    if found and os.path.realpath(os.path.dirname(found)) == scripts:
        print("installed. Run:  noesis")
    else:
        print("installed to %s, which is not on your PATH." % scripts)
        print("Either add it, or run the same thing as:  %s -m agent" % sys.executable)
    print("First run opens the setup wizard and probes your model endpoint before writing a key.")


if __name__ == "__main__":
    main()
