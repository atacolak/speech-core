{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = with pkgs; [
    cargo
    rustc
    rustfmt
    clippy
    pkg-config
    alsa-lib
    gcc
  ];

  # Keep alsa-sys/cpal builds boring on NixOS. nix-shell usually wires this
  # automatically via setup hooks, but the explicit value makes ad-hoc shells
  # and remote ssh invocations less haunted.
  PKG_CONFIG_PATH = "${pkgs.alsa-lib.dev}/lib/pkgconfig";
}
