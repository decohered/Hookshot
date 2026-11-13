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
  # The game's FFmpeg, static and position independent, configured as scripts/linux/deps.sh configures the AppImage's: it
  # goes into the game's executable, so a game linked in this shell needs no libavcodec.so at run time. nixpkgs' ffmpeg is
  # the same release (9.0.2) and its source; its shared, every-feature configuration is not used.
  ffmpegStatic = pkgs.stdenv.mkDerivation {
    pname = "ffmpeg-static";
    inherit (pkgs.ffmpeg) version src;
    nativeBuildInputs = [ pkgs.pkg-config pkgs.nasm ];
    buildInputs = [ dav1d ];
    # (nixpkgs' ffmpeg passes no --build or --host either)
    configurePlatforms = [ ];
    configureFlags = [
      "--pkg-config-flags=--static"
      "--enable-static"
      "--disable-shared"
      "--enable-pic"
      "--disable-programs"
      "--disable-doc"
      "--disable-network"
      "--disable-autodetect"
      "--disable-everything"
      "--disable-avformat"
      "--disable-avfilter"
      "--disable-avdevice"
      "--enable-avcodec"
      "--enable-swscale"
      "--enable-swresample"
      "--enable-avutil"
      "--enable-libdav1d"
      "--enable-decoder=vp8,vp9,libdav1d"
      "--enable-parser=vp8,vp9,av1"
    ];
  };
  # The prefix the game links: FFmpeg's libraries with the static dav1d beside them, since libavcodec's AV1 decoder is
  # libdav1d and nx2/build.py adds lib/libdav1d.a only when the prefix holds it.
  ffmpegPrefix = pkgs.symlinkJoin {
    name = "ffmpeg-prefix";
    paths = [ ffmpegStatic dav1d ];
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
  # nixpkgs' Vulkan loader: libvulkan.so.1 for the game's dlopen, libvulkan.so for the linker's -lvulkan.
  vulkanLoaderLib = "${pkgs.vulkan-loader}/lib";
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
    HOOKSHOT_FFMPEG = "${ffmpegPrefix}";
    HOOKSHOT_VULKAN_HEADERS = "${pkgs.vulkan-headers}/include";
    HOOKSHOT_SPIRV_HEADERS = "${pkgs.spirv-headers}/include";
    HOOKSHOT_VMA_HEADERS = "${pkgs.vulkan-memory-allocator}/include";
    # The game loads the Vulkan loader by name (libvulkan.so.1) at run time. Its RUNPATH is this shell's output, which has
    # no library folder, and NixOS has no loader on the default search path, so the library path names nixpkgs' loader.
    # The linker (-lvulkan, tests/test_present_aspect_vulkan.py) searches LIBRARY_PATH, which takes the same folder.
    LD_LIBRARY_PATH = vulkanLoaderLib;
    LIBRARY_PATH = vulkanLoaderLib;
  };
}
