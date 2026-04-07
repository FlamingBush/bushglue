# Text-to-Verse
**WARNING: this is almost entirely generated code. Have I read all of it? NO. Is it pretty simple? YES. Check the PROJECT.md for the input spec. Check the PLAN.md for the generated high level plan.**

Text-to-Verse (t2v) takes a plain text input, finds a matching verse (or other text snippet), and optionally reinterprets it using a role-playing affect. It relys on an embedding database running in ChromaDB to store and retrieve snippets, for access to the embedding and language models it requires access to ollama. It can be run as a standalone CLI command in one shot mode, or it can be launched as server with an HTTP+JSON API.

## Prerequisites
### Install Rust
```bash
$ curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### Install text-to-verse
```bash
$ cd text-to-verse
# This could take a while
$ cargo install --path .
```

### Ollama
* Install Ollama
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

* Get the models that text-to-verse needs
```bash
text-to-verse list-models | xargs -n 1 ollama pull
```

### Install ChromaDB
* Install UV
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
* Create a virtual environment and install ChromaDB
```bash
mkdir t2v-chroma
cd t2v-chroma
uv venv
source .venv/bin/activate
uv pip install chromadb
```

* Copy the chroma DB into `t2v-chroma`
This assumes you have a `chroma` directory with the DB files in the current directory.
```bash
cp -r chroma t2v-chroma/
```

### Running ChromaDB
* Start ChromaDB
```bash
chroma run --path t2v-chroma
```


## Example Usage

Single shot CLI usage using all default behaviors (llm chosen collection, no affect):
```bash
$ text-to-verse query "what should I do when I feel lost"
Sometimes, when you're feeling helpless, the secret is to help someone else. Get out of your own head. Trust me. The next time someone asks for help, say yes.
```

Single shot CLI usage with a specific affect:
```bash
$ text-to-verse query --affects-dir templates/affects list-affects
biblical
circus
cultist
explorer
$ text-to-verse query --affects-dir templates/affects query "what should I do when I feel lost" --affect cultist
Hail the stormy heavens, and the starry hosts shall come down upon us, bearing the sign of doom.
```

Single shot CLI usage with a random affect:
```bash
$ text-to-verse query --affects-dir templates/affects --random-affect "what should I do when I feel lost" 
I couldn't find my way. Hold on, I'll take you there.
```

Single shot CLI usage with a specific collection:
```bash
$ text-to-verse list-collections
verse_embeddings
movie_quotes
$ text-to-verse query --affects-dir templates/affects --collection movie_quotes "what should I do when I feel lost"
There is always hope.
```

Single shot CLI usage with a random collection:
```bash
$ text-to-verse query --affects-dir templates/affects --random-collection "what should I do when I feel lost"
For behold the battle is before us, and the water of the Jordan on this side and on that side, and banks, and marshes, and woods: and there is no place for us to turn aside.
```

Server mode usage:
```bash
$ text-to-verse serve --port 8080 --affects-dir templates/affects
```
