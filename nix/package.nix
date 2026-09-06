# Shared package expression for principle-graph. Consumed by the project
# flake (packages.principle-graph) and by the pi-workspace-osh-v2 image flake
# (~/.pi flake.nix vendors this file via git archive of HEAD, so `src = ./..`
# resolves correctly from both layouts).
{ python312Packages }:
let
  # pyproject.toml pins the driver contract to neo4j>=5.0,<6 (host test suite
  # validated against 5.28.5); nixpkgs unstable now ships 6.x, so pin the last
  # 5.x release here. MAINTENANCE: manual pin — flake.lock bumps will not
  # change this; revisit when pyproject's <6 cap is lifted.
  neo4j = python312Packages.buildPythonPackage rec {
    pname = "neo4j";
    version = "5.28.5";
    format = "wheel";
    src = python312Packages.fetchPypi {
      inherit pname version;
      format = "wheel";
      python = "py3";
      dist = "py3";
      hash = "sha256-takZu3vi3QzLcqzVJySWS3Pxu19cdToDGvhNu+Fw+/M=";
    };
    dependencies = [ python312Packages.pytz ];
    doCheck = false; # wheel-only install, no tests shipped
    pythonImportsCheck = [ "neo4j" ];
    meta = {
      description = "Neo4j Bolt Driver for Python (pinned 5.x for pyproject <6 contract)";
      license = python312Packages.lib.licenses.asl20;
    };
  };
in
python312Packages.buildPythonApplication rec {
  pname = "principle-graph";
  version = "0.1.0";
  pyproject = true;
  src = ./..;
  build-system = [ python312Packages.setuptools ];
  dependencies = [ neo4j python312Packages.pymupdf ];
  pythonImportsCheck = [ "principle_graph" ];
  meta = {
    description = "Local knowledge graph memory prototype";
    mainProgram = "pg";
  };
}
