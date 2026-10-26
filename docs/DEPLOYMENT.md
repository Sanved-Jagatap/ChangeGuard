# Deployment

Goal: a public URL where anyone can run scenarios safely and cheaply. Steps that need your accounts are in `docs/USER_TODO.md` section 7.

## Recommended shape
- **Database:** a free hosted PostgreSQL (for example Neon). Run `changeguard init-db` once with the admin URL; it migrates, seeds, and creates the three least-privilege roles.
- **API:** a container host that runs the `Dockerfile` (Render, Fly.io, Railway). Set the environment variables from `.env.example`.
- **UI:** a second service using the same image with command `streamlit run src/changeguard/ui/app.py --server.port $PORT --server.address 0.0.0.0` and `CG_API_URL` pointing at the API (Streamlit Community Cloud also works with this repo).

## Settings that matter for a public demo
| Variable | Recommendation |
|---|---|
| `CG_ANONYMOUS_ROLE` | `viewer` (read-only) unless you accept that anyone can start hosted-model runs; `analyst` allows runs but still cannot approve or execute |
| `CG_DEMO_TOKENS` | replace every `change-me-*` with long random values; give the reviewer token only to people who should execute |
| `CG_MAX_AGENT_RUNS_PER_DAY` | small (for example 40); runs beyond it are refused |
| provider-side budget | set a hard spend cap/alert in the provider console; app limits are a second line of defence |
| `CG_ALLOW_RESET` | `1` only on the sandbox, so an admin can restore the seed data |
| `CG_DATABASE_URL_READER/RECORDER/SANDBOX` | set all three so the least-privilege roles are actually used |

## After deploying
1. Hit `/health`, then run the three scenarios from the UI.
2. Take screenshots/GIF of the trace timeline and the approval panel.
3. Put the live URL at the top of `README.md` and replace "not deployed yet".
