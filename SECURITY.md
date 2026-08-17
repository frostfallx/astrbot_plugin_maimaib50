# Security and privacy

Do not include private deployment configuration in issues, pull requests or
commits. In particular, never commit:

- LLM API keys;
- Diving-Fish or Lxns developer tokens;
- QQ login credentials or account exports;
- Home Assistant, NapCat or AstrBot tokens;
- `user_sources.json`, `daily_usage.json` or render caches;
- locally downloaded assets, aliases or score/statistics datasets.

The repository defaults and tests contain placeholders only. Production
secrets must be stored in AstrBot plugin configuration.

If a secret is exposed, revoke or rotate it immediately. Removing it from the
latest commit is not sufficient because Git history and forks may retain it.

Report a vulnerability privately to the repository owner rather than opening
a public issue containing exploit details or credentials.
