{ pkgs, lib, ... }:

let
  # requirements.txt (the pins are for the Windows and AppImage builds; nixpkgs' versions are what this shell gets) and
  # nx2/prompt_requirements.txt, where nixpkgs packages them. astc-encoder-py and texture2ddecoder are not in this nixpkgs.
  python = pkgs.python3.withPackages (ps: [
    ps.lz4
    ps.numpy
    ps.capstone
    ps.unicorn
    ps.zstandard
    ps.tkinter
    ps.pillow
    ps.fonttools
  ]);
  # nixpkgs splits dav1d into dev (the header) and out (the library), and builds it shared only. tests/test_dav1d_av1.py
  # links a prefix's lib/libdav1d.a, as scripts/linux/deps.sh builds dav1d (static: it goes into the game's executable).
  dav1dStatic = pkgs.dav1d.overrideAttrs (old: {
    mesonFlags = old.mesonFlags ++ [ "--default-library=static" ];
  });
  dav1d = pkgs.symlinkJoin {
    name = "dav1d";
    paths = [ dav1dStatic.dev dav1dStatic ];
  };
  # nixpkgs' clang is a cc-wrapper for the host only: it adds host-only flags (a dynamic linker, -fPIC) that another target
  # rejects, so tests/test_link_path_comma.py's MSVC and MinGW links fail through it. A clang or clang++ that names another
  # target runs the unwrapped clang of the same release, as Ubuntu's clang does; every other invocation runs the wrapper.
  clangRoute = name: pkgs.writeShellScriptBin name ''
    target=x86_64-unknown-linux-gnu
    next=
    for arg in "$@"; do
      if [ -n "$next" ]; then
        target=$arg
        next=
      else
        case "$arg" in
          -target) next=1 ;;
          --target=*) target=''${arg#--target=} ;;
        esac
      fi
    done
    if [ "$target" = x86_64-unknown-linux-gnu ]; then
      exec ${pkgs.clang}/bin/${name} "$@"
    fi
    exec ${pkgs.clang.cc}/bin/${name} "$@"
  '';
  # One folder for both, as in nixpkgs' clang: a tool finds clang beside clang++ (tests/test_game_font.py). It outranks
  # pkgs.clang's bin/clang and bin/clang++ in the profile (lower priority number wins a collision).
  clangRoutes = lib.meta.setPrio (-1) (pkgs.symlinkJoin {
    name = "clang-routes";
    paths = [ (clangRoute "clang") (clangRoute "clang++") ];
  });
in
{
  packages = [
    python
    pkgs.bash
    pkgs.git
    pkgs.gcc
    pkgs.clang
    clangRoutes
    pkgs.lld
    pkgs.pkg-config
    pkgs.jq
    pkgs.ccache
    pkgs.vulkan-tools
    pkgs.spirv-tools
    pkgs.glslang
    pkgs.dotnet-sdk
  ];

  # nixpkgs' compilers add hardening flags (-fPIC, _FORTIFY_SOURCE even without -O, stack protection, ...) that the
  # upstream LLVM and Ubuntu toolchains the build and CI use do not add. Glibc's _FORTIFY_SOURCE #warning fails the tests'
  # -Werror builds, so all of them are off.
  hardeningDisable = [ "all" ];

  env = {
    HOOKSHOT_DAV1D = "${dav1d}";
    HOOKSHOT_VULKAN_HEADERS = "${pkgs.vulkan-headers}/include";
    HOOKSHOT_SPIRV_HEADERS = "${pkgs.spirv-headers}/include";
    HOOKSHOT_VMA_HEADERS = "${pkgs.vulkan-memory-allocator}/include";
    PKG_CONFIG_PATH = lib.makeSearchPath "lib/pkgconfig" [ pkgs.ffmpeg.dev ];
  };
}
