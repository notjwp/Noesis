#!/bin/sh
# Install NOESIS from a git checkout, and say where the command landed.
# POSIX sh: `sh` is dash on Debian and bash on Git for Windows, and it has to be
# the same script on both.
#
# One line from anywhere, no `curl | sh` - the policy gate in agent/policy.py
# classifies that shape as destructive, and an installer this project ships
# should be one it would run:
#
#   git clone https://github.com/notjwp/Noesis.git && cd Noesis && sh scripts/install.sh
#
# A CHECKOUT, not a package: `noesis --update` is `git pull` in this tree, so a
# `pip install git+...` would install something that cannot update itself.
# Editable, so the pull IS the update. Idempotent: run it again after a pull
# and pip reports the requirements already satisfied.
set -eu

REPO="https://github.com/notjwp/Noesis.git"

# The interpreter. An active virtualenv's own python, by path, before any PATH
# lookup: a Windows venv has no python3.exe, so `python3` there resolves to the
# Microsoft Store alias and installs into a Python nobody chose. Then `python`
# before `python3` for the same reason, and `py -3` last - the Windows
# launcher, the only one guaranteed on PATH after a python.org install.
python=""; pyargs=""
if [ -n "${VIRTUAL_ENV:-}" ]; then
    for candidate in "$VIRTUAL_ENV/bin/python" "$VIRTUAL_ENV/Scripts/python.exe"; do
        [ -x "$candidate" ] && { python="$candidate"; break; }
    done
    [ -n "$python" ] || { echo "VIRTUAL_ENV is set to $VIRTUAL_ENV but no python was found inside it." >&2; exit 1; }
else
    for candidate in python python3 py; do
        args=""; [ "$candidate" = py ] && args="-3"
        if "$candidate" $args -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
            python="$candidate"; pyargs="$args"; break
        fi
    done
fi
[ -n "$python" ] || { echo "NOESIS needs Python 3.12 or newer on PATH (python, python3, or py -3)." >&2; exit 1; }
"$python" $pyargs -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || { echo "NOESIS needs Python 3.12 or newer; $python is older." >&2; exit 1; }
command -v git >/dev/null || { echo "NOESIS needs git on PATH: the install is a checkout and updates are a pull." >&2; exit 1; }

# Inside the checkout already, or clone one here. `name = "noesis"`
# is the line that says this pyproject is ours and not some other project's.
if [ -f pyproject.toml ] && grep -q '^name = "noesis"' pyproject.toml; then
    :
elif [ -f Noesis/pyproject.toml ]; then
    cd Noesis
else
    git clone "$REPO" Noesis
    cd Noesis
fi
echo "installing from $(pwd) with $python $pyargs"

# Into the active virtualenv if there is one, else the user site. Never the
# system site-packages: that needs root and breaks the distro's own Python.
if [ -n "${VIRTUAL_ENV:-}" ]; then
    "$python" $pyargs -m pip install -e .
    scripts_dir="$("$python" $pyargs -c 'import sysconfig; print(sysconfig.get_path("scripts"))')"
else
    "$python" $pyargs -m pip install --user -e .
    scripts_dir="$("$python" $pyargs -c 'import sysconfig, os; print(sysconfig.get_path("scripts", os.name + "_user"))')"
fi

# The command THIS install wrote, and whether PATH reaches it. Python walks
# PATH itself so the answer is right on Windows too, where the shell's PATH is
# spelled differently from the one the interpreter sees - and it must be this
# install's `noesis`, not a stale one somewhere else on PATH.
reach="$("$python" $pyargs - "$scripts_dir" <<'PY'
import os, shutil, sys
scripts = os.path.realpath(sys.argv[1])
here = next((f for f in ("noesis", "noesis.exe") if os.path.isfile(os.path.join(scripts, f))), None)
found = shutil.which("noesis")
print("missing" if here is None
      else "on-path" if found and os.path.realpath(os.path.dirname(found)) == scripts
      else "off-path")
PY
)"
echo
case "$reach" in
    on-path)  echo "installed. Run:  noesis" ;;
    off-path) echo "installed to ${scripts_dir}, which is not on your PATH."
              echo "Either add it, or run the same thing as:  $python $pyargs -m agent" ;;
    *)        echo "pip finished but wrote no noesis command into ${scripts_dir}." >&2; exit 1 ;;
esac
echo "First run opens the setup wizard and probes your model endpoint before writing a key."
