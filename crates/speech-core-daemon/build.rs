use std::path::PathBuf;

fn main() {
    let external = std::env::var("TRANSCRIBE_CPP_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/home/sf/workspace/external/transcribe.cpp"));
    let include_dir = external.join("include");
    let build_dir = external.join("build");

    println!("cargo:rerun-if-env-changed=TRANSCRIBE_CPP_DIR");
    println!("cargo:rerun-if-changed=native/transcribe_shim.cpp");
    println!("cargo:rerun-if-changed=native/transcribe_shim.h");

    cc::Build::new()
        .cpp(true)
        .std("c++17")
        .define("TRANSCRIBE_STATIC", None)
        .include(&include_dir)
        .file("native/transcribe_shim.cpp")
        .compile("speech_core_transcribe_shim");

    println!(
        "cargo:rustc-link-search=native={}",
        build_dir.join("src").display()
    );
    println!(
        "cargo:rustc-link-search=native={}",
        build_dir.join("ggml/src").display()
    );
    println!("cargo:rustc-link-lib=static=transcribe");
    println!("cargo:rustc-link-lib=static=ggml");
    println!("cargo:rustc-link-lib=static=ggml-cpu");
    println!("cargo:rustc-link-lib=static=ggml-base");

    if cfg!(target_os = "linux") {
        println!("cargo:rustc-link-lib=dylib=stdc++");
        println!("cargo:rustc-link-lib=dylib=dl");
        println!("cargo:rustc-link-lib=dylib=m");
        println!("cargo:rustc-link-lib=dylib=pthread");
    }
}
