{
  description = "principle-graph - local knowledge graph memory prototype";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems =
        f:
        builtins.listToAttrs (
          map (system: {
            name = system;
            value = f system;
          }) systems
        );
    in
    {
      packages = forAllSystems (
        system:
        let
        pkgs = import nixpkgs { inherit system; };

        principle-graph = pkgs.callPackage ./nix/package.nix { };
      in
      {
        default = principle-graph;
        principle-graph = principle-graph;
      }
    );

    apps = forAllSystems (system: {
      default = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/pg";
      };
    });

    devShells = forAllSystems (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
      in
      {
        default = pkgs.mkShell {
          packages = [
            (python.withPackages (
              ps: with ps; [
                neo4j
                pymupdf
                pytest
              ]
            ))
            pkgs.neo4j
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            echo "principle-graph dev shell (python ${python.version})"
          '';
        };
      }
    );
    };
}
