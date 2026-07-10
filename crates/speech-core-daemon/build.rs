use std::path::PathBuf;
use std::process::Command;

fn main() {
    emit_build_identity();
    let external = std::env::var("TRANSCRIBE_CPP_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").unwrap_or_else(|_| "/home".into());
            PathBuf::from(home).join("workspace/external/transcribe.cpp")
        });
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

fn emit_build_identity() {
    println!("cargo:rerun-if-env-changed=SPEECH_CORE_BUILD_GIT_COMMIT");
    println!("cargo:rerun-if-env-changed=SPEECH_CORE_BUILD_GIT_DIRTY");
    let commit = std::env::var("SPEECH_CORE_BUILD_GIT_COMMIT")
        .ok()
        .or_else(|| git_output(["rev-parse", "HEAD"]));
    let dirty = std::env::var("SPEECH_CORE_BUILD_GIT_DIRTY")
        .ok()
        .or_else(|| git_dirty().map(|dirty| dirty.to_string()));

    println!(
        "cargo:rustc-env=SPEECH_CORE_BUILD_GIT_COMMIT={}",
        commit.unwrap_or_else(|| "unknown".to_owned())
    );
    println!(
        "cargo:rustc-env=SPEECH_CORE_BUILD_GIT_DIRTY={}",
        dirty.unwrap_or_else(|| "unknown".to_owned())
    );
    println!(
        "cargo:rustc-env=SPEECH_CORE_BUILD_TARGET={}",
        std::env::var("TARGET").unwrap_or_else(|_| "unknown".to_owned())
    );
    println!(
        "cargo:rustc-env=SPEECH_CORE_BUILD_PROFILE={}",
        std::env::var("PROFILE").unwrap_or_else(|_| "unknown".to_owned())
    );
}

fn git_dirty() -> Option<bool> {
    let output = Command::new("git")
        .args(["status", "--porcelain"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(!output.stdout.is_empty())
}

fn git_output<const N: usize>(args: [&str; N]) -> Option<String> {
    let output = Command::new("git").args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    let trimmed = text.trim();
    (!trimmed.is_empty()).then(|| trimmed.to_owned())
}
