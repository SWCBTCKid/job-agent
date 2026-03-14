# Job Agent

## Run

```powershell
cd "D:\Claude Trading Agent\job-agent"
python main.py --dry-run
```

## Notes

- Uses Greenhouse, Lever, and HN out of the box.
- Workday/Wellfound/YC/Pragmatic/LinkedIn connectors are implemented as best-effort parsers driven by `data/source_seeds.json`.
- If `ANTHROPIC_API_KEY` is unset, Stage 2 uses deterministic fallback scoring.
- Add candidate resume text in `data/resume.txt`.
- Stage 1 uses semantic embeddings (`sentence-transformers` first, `voyage` API fallback) and falls back to lexical TF-IDF when needed.
