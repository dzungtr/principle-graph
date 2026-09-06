# Shared package expression for principle-graph. Consumed by the project
# flake (packages.principle-graph) and by the pi-workspace-osh-v2 image flake
# (~/.pi flake.nix vendors this file via git archive of HEAD, so `src = ./..`
# resolves correctly from both layouts).
{ python312Packages }:
python312Packages.buildPythonApplication rec {
  pname = "principle-graph";
  version = "0.1.0";
  pyproject = true;
  src = ./..;
  build-system = [ python312Packages.setuptools ];
  dependencies = with python312Packages; [ neo4j pymupdf ];
  pythonImportsCheck = [ "principle_graph" ];
  meta = {
    description = "Local knowledge graph memory prototype";
    mainProgram = "pg";
  };
}
