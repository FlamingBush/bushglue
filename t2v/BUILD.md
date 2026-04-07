# Building on Windows

## Prerequisites

### 1. Rust toolchain

Install via [rustup](https://rustup.rs/):

```powershell
winget install Rustlang.Rustup
```

Or download `rustup-init.exe` directly from https://rustup.rs and run it.

After installation, open a new terminal and verify:

```powershell
rustc --version
cargo --version
```

The default toolchain (`stable-x86_64-pc-windows-msvc`) is recommended. It uses the MSVC linker which is included with Visual Studio Build Tools.

### 2. MSVC Build Tools

If you don't have Visual Studio installed, install the Build Tools alone:

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools
```

During installation, select **"Desktop development with C++"**. This provides the MSVC linker that Rust requires on Windows.

### 3. Runtime dependencies (not needed to build, needed to run)

- **Ollama** — https://ollama.com/download — serves the embedding and reranking models
- **ChromaDB** — requires Python: `pip install chromadb` then `chroma run --path ./chromadb`

---

## Build

```powershell
# Clone the repo
git clone git@github.com:na-g/t2v.git
cd t2v

# Debug build
cargo build

# Release build (recommended for running)
cargo build --release
```

The binary is placed at:
- Debug: `target\debug\text-to-verse.exe`
- Release: `target\release\text-to-verse.exe`

---

## Run

```powershell
# Start the server (release build)
.\target\release\text-to-verse.exe serve --port 8080

# One-shot query
.\target\release\text-to-verse.exe query "what should I do when I feel lost"
```

The chat UI is available at http://localhost:8080/chat once the server is running.

---

## Notes

- `reqwest` (the HTTP client) uses Windows' built-in TLS (SChannel) on Windows — no OpenSSL installation is required.
- All other dependencies are pure Rust and have no native library requirements.
- Build times are similar to Linux/macOS; the first build downloads and compiles all crates from crates.io.
