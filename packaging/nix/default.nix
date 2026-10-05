# Use the same expression for both clients. Runtime dependency constraints
# are checked by Nix's Python hooks; none are relaxed or removed.
{ pkgs ? import <nixpkgs> { }, xrdclientSrc ? ../.., xgfalclientSrc ? null }:
let
  py = pkgs.python312Packages;
  version = src: name:
    let
      lines = pkgs.lib.splitString "\n" (builtins.readFile (src + "/src/${name}/_version.py"));
      line = builtins.head (builtins.filter (pkgs.lib.hasPrefix "__version__ = ") lines);
    in builtins.head (builtins.match "__version__ = \"([0-9]+\\.[0-9]+\\.[0-9]+)\"" line);
  common = with py; [ asn1crypto botocore cryptography pyjwt urllib3 ];
  make = name: src: deps: py.buildPythonPackage {
    pname = name;
    version = version src name;
    src = pkgs.lib.cleanSource src;
    pyproject = true;
    build-system = [ py.hatchling ];
    # The upper bound works around Twine's metadata inspection, which Nix
    # doesn't use. Change only this build-tool bound, not runtime requirements.
    postPatch = ''
      substituteInPlace pyproject.toml --replace-fail "hatchling>=1.27,<1.32" "hatchling>=1.27"
    '';
    dependencies = common ++ deps;
    nativeCheckInputs = with py; [ pytestCheckHook pytest-timeout pytest-xdist pytest-cov fsspec ];
    # Complexipy isn't packaged by this Nixpkgs release. Its repository-wide
    # maintainability gate stays in CI; omit only that development-tool test.
    disabledTestPaths = pkgs.lib.optionals (name == "xrdclient") [ "tests/test_maintainability.py" ];
    disabledTestMarks = [ "interop" "parity" ];
    pytestFlags = [ "-q" "-n" "4" "--timeout=300" "-p" "no:cacheprovider" ];
    pythonImportsCheck = [ name ];
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    meta = {
      description = "Python storage client";
      homepage = "https://github.com/rob-c/${name}";
      license = pkgs.lib.licenses.lgpl3Plus;
      platforms = pkgs.lib.platforms.unix;
    };
  };
  xrdclient = make "xrdclient" xrdclientSrc [ ];
  xgfalclient = make "xgfalclient" xgfalclientSrc [ xrdclient ];
in {
  inherit xrdclient;
} // pkgs.lib.optionalAttrs (xgfalclientSrc != null) {
  inherit xgfalclient;
  nixosTest = pkgs.testers.runNixOSTest {
    name = "storage-clients";
    nodes.machine = { ... }: {
      environment.systemPackages = [ xrdclient xgfalclient ];
    };
    testScript = ''
      import json
      start_all()
      machine.wait_for_unit("multi-user.target")
      machine.succeed("printf 'physics data' > /tmp/source")
      for tool in ("xrd-cp", "gfal-copy"):
          report = json.loads(machine.succeed(f"{tool} --output-format json /tmp/source /tmp/{tool}"))
          assert report["summary"]["ok"]
          machine.succeed(f"cmp /tmp/source /tmp/{tool}")
      for tool in ("xrd-fs", "gfal-ls"):
          report = json.loads(machine.succeed(f"{tool} --output-format json --help"))
          assert report["summary"]["exit_code"] == 0
          machine.succeed(f"{tool} --output-format xml --help | grep 'schema=\"storage-client-report\"'")
    '';
  };
}
