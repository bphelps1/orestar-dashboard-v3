# Pipeline data

Generated datasets and scraper checkpoints are intentionally not stored in
Git. GitHub Actions hydrates the required profiles from Supabase Storage with
`scripts/pipeline_state.py` before work and publishes a content-addressed,
checksummed generation afterward.

For local pipeline work, create the usual `.env` and run:

```bash
python scripts/pipeline_state.py pull transactions summaries auxiliary
```

The live dashboard reads aggregate data from Supabase Postgres. Nothing under
this directory is part of the Vercel deployment.
