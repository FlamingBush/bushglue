# Prompt Templates

Prompt files use Python f-string format for variable substitution. Multiple prompts within a single file are separated by a line containing only `---`.

## isolate.txt

Selects the most interesting/orthogonal verse from a set of candidates.

| Variable | Description |
|----------|-------------|
| `{num_verses}` | Number of candidate verses presented |
| `{verses_block}` | Formatted list of verses, each as `[verse_id] (bible_verse): text` |

**JSON schema for response:**
```json
{
  "type": "object",
  "properties": {
    "verse_id": {"type": "string"}
  },
  "required": ["verse_id"]
}
```

## modernize.txt

Converts a bible verse into modern english.

| Variable | Description |
|----------|-------------|
| `{verse_text}` | Original verse text |
| `{verse_reference}` | Verse reference (e.g., "Genesis 1:1") |

**JSON schema for response:**
```json
{
  "type": "object",
  "properties": {
    "modern_text": {"type": "string"}
  },
  "required": ["modern_text"]
}
```

## questionize.txt

Generates questions that a verse could answer.

| Variable | Description |
|----------|-------------|
| `{modern_text}` | Modern english version of the verse |
| `{verse_reference}` | Verse reference (e.g., "Genesis 1:1") |
| `{num_questions}` | Number of questions to generate |

**JSON schema for response:**
```json
{
  "type": "object",
  "properties": {
    "questions": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["questions"]
}
```
