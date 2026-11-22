# Gyan chat UI

The web chat for Gyan: [LibreChat](https://github.com/danny-avila/LibreChat) (MIT license, open
source), configured and branded for Sarastra Labs, talking to the Gyan gateway.

```
browser ──► LibreChat (login, chats, MongoDB) ──► gateway (/v1, API key, limits) ──► model / research
```

## Why no forked source code

LibreChat 0.8.9 can be themed from its config file, so Gyan's branding is **configuration plus a
few assets**, not a modified copy of LibreChat. Upgrading is just a new image tag, with no merge
conflicts. If we ever need something the config cannot do, that is the moment to fork.

| What | Where |
|---|---|
| Colors, radii, fonts (Sarastra Labs look) | `librechat.yaml`, section `interface.theme` |
| Welcome text, which features are hidden | `librechat.yaml`, section `interface` |
| The two choices people see (Gyan Chat, Gyan Research) | `librechat.yaml`, section `modelSpecs` |
| Connection to the gateway | `librechat.yaml`, section `endpoints.custom` |
| Logo, favicons, model avatar | `assets/` (made from the company logo by `scripts/make_brand_assets.py`) |
| Browser tab title and color | `entrypoint.sh` |
| App name, footer, help link | environment variables in `docker-compose.yml` |

The look comes from sarastralabs.com: paper `#FBF7F2`, ink `#16130F`, orange `#FF5C00`, Inter font,
pill-shaped buttons, with a warm dark mode.

## Run it

1. Start the gateway stack and create the UI's key (once):

   ```bash
   python scripts/init_env.py
   docker compose up -d --build
   docker compose exec gateway gyan-gateway create-key --name gyan-ui --rpm 300 --daily 5000
   ```

   Put the key it prints (shown once) in `.env` as `GYAN_API_KEY=`.

2. Start the UI:

   ```bash
   docker compose --profile ui up -d
   ```

   Open <http://localhost:3080>.

3. **Create the admin account.** With `ALLOW_REGISTRATION=true` in `.env`, register: the first user
   becomes the admin. Then set `ALLOW_REGISTRATION=false` and run
   `docker compose --profile ui up -d` again, so strangers cannot sign up. Add people later by
   turning registration on briefly, or through LibreChat's admin tools.

## Good to know

- **All users share one gateway key** (`gyan-ui`), so the gateway's per-key limits apply to the UI as
  a whole. Raise `--rpm` if many people chat at once. Per-person limits are LibreChat's own
  (its `*_MAX` environment variables).
- **Titles:** chat titles are not auto-generated (`titleConvo: false`), because each title costs a
  model call and the 2-core server serves one request at a time.
- **No uploads:** the models are text-only, so file attachments are switched off.
- **Fonts:** only the fonts bundled with LibreChat (Inter, Roboto Mono) are available through the
  theme config, so headings use Inter rather than Inter Tight.
- **Change the look:** edit the colors in `librechat.yaml`, then
  `docker compose --profile ui restart librechat`. Icons: replace the logo in `brand/`, run
  `python ui/scripts/make_brand_assets.py --src ui/brand/logo-on-dark.png --out ui/assets`, restart.
- **Upgrade LibreChat:** change the image tag in `docker-compose.yml`, check its
  [release notes](https://github.com/danny-avila/LibreChat/releases) for config changes
  (`version:` in `librechat.yaml`), and restart.
- **License:** LibreChat is MIT licensed; keep its license notice. The Open WebUI branding limits
  we met earlier do not apply here.
