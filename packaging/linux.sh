#!/usr/bin/env bash
# Executed only INSIDE a disposable container. Never run on the host.
set -euo pipefail
test -d /src/xrdclient
test -d /artifacts
family=$1
interpreter=$2
shift 2
if [[ "$family" == rpm ]]; then
    dnf -y install "$@"
    dnf clean all
else
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "$@"
fi
cp /etc/os-release /artifacts/os-release
"$interpreter" --version > /artifacts/python-version.txt
mkdir /work
# Explicit source list: don't copy .git, credentials, venvs or build caches.
for client in xrdclient xgfalclient; do
    mkdir "/work/$client"
    for entry in src tests tools benchmarks examples packaging share .github .gitignore pyproject.toml README.md LICENSE CHANGELOG.md maintainability.json; do
        if [[ -e "/src/$client/$entry" ]]; then
            cp -R "/src/$client/$entry" "/work/$client/"
        fi
    done
done
"$interpreter" -m venv /work/build-env
buildpy=/work/build-env/bin/python
"$buildpy" -m pip install --only-binary=:all: 'pip>=24' 'build>=1,<2' 'hatchling>=1.27,<1.32' 'twine>=6,<7'
mkdir /artifacts/wheels /artifacts/wheelhouse
for client in xrdclient xgfalclient; do
    "$buildpy" -m build --no-isolation --outdir /artifacts/wheels "/work/$client"
done
"$buildpy" -m twine check --strict /artifacts/wheels/*
"$buildpy" -m pip download --only-binary=:all: --dest /artifacts/wheelhouse /artifacts/wheels/*.whl
"$interpreter" -m venv /work/runtime
runtimepy=/work/runtime/bin/python
"$runtimepy" -m pip install --no-index --only-binary=:all: --find-links=/artifacts/wheelhouse /artifacts/wheels/*.whl
"$runtimepy" -m pip check
"$runtimepy" -m pip inspect > /artifacts/dependencies.json
"$runtimepy" /work/xrdclient/tools/installed_smoke.py --bin-dir /work/runtime/bin
"$buildpy" /work/xrdclient/tools/distribution_package.py --family "$family" --wheelhouse /artifacts/wheelhouse --output /artifacts/packages --python "$(command -v "$interpreter")"
if [[ "$family" == rpm ]]; then
    dnf -y install /artifacts/packages/*.rpm
else
    apt-get install -y /artifacts/packages/*.deb
fi
"$interpreter" -I -S /opt/storage-clients/launch.py --smoke /work/xrdclient/tools/installed_smoke.py --bin-dir /usr/bin
# Run all hermetic/fault/CLI/VOMS tests as an ordinary user: root would mask
# permission failures. Real-server interoperability remains a separate CI gate.
"$runtimepy" -m pip install --only-binary=:all: pytest pytest-cov pytest-timeout pytest-xdist fsspec
maintainability=()
if ! "$runtimepy" -m pip install --only-binary=:all: 'complexipy>=6,<7' 'radon>=6,<7'; then
    # A pre-release interpreter (Fedora Rawhide) can predate the complexipy
    # wheels. That development-tool gate still runs in CI on released Pythons;
    # every runtime test still runs here.
    maintainability=(--ignore=tests/test_maintainability.py)
fi
useradd --create-home tester
mkdir /artifacts/tests
# Keep the bind-mounted parent owned by the runner so it can record results
# after the container exits, including when a test process is killed.
chown -R tester /work /artifacts/tests
suite_status=0
for client in xrdclient xgfalclient; do
    cd "/work/$client"
    runuser -u tester -- "$runtimepy" -m pytest -q -p no:cacheprovider -n 4 -m 'not interop and not parity' --timeout=300 --cov --cov-fail-under=0 --cov-report="xml:/artifacts/tests/$client-coverage.xml" --junitxml="/artifacts/tests/$client-tests.xml" "${maintainability[@]}" || suite_status=1
done
# Package-manager removal must remove the commands and leave no private runtime.
if [[ "$family" == rpm ]]; then
    rpm -e xgfalclient xrdclient
else
    dpkg --purge xgfalclient xrdclient
fi
test ! -e /usr/bin/xrd-cp
test ! -e /usr/bin/gfal-copy
test ! -e /opt/storage-clients/launch.py
exit "$suite_status"
