# Secret Rotation Checklist

Use this checklist if `.env`, `config.toml`, local credential folders, `data/`, browser profiles, or release bundles were exposed.

1. Identify whether the beta instance, prod instance, or both were affected. Do not assume they share tokens or storage.
2. Rotate the Telegram bot token for the affected instance and update only that instance's runtime config.
3. Revoke and recreate any exposed third-party credentials or token files.
4. Reset UClass or UOS portal credentials and clear any stale web/API tokens stored in runtime state.
5. Rotate onboarding or relay shared secrets and verify the public onboarding route still points at the intended instance.
6. Rebuild affected browser profiles if browser profile artifacts were exposed.
7. Replace leaked release bundles, confirm `.env`, `data/`, local credential folders, DB files, and lock files are absent from the new stage, then redeploy.
