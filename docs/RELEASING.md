# Releasing viper-pm

Everything is automated by `.github/workflows/release.yml`. Two one-time
setups, then every release is just pushing a tag.

## One-time setup A — PyPI (2 minutes, required for `pip install viper-pm`)

Uses **Trusted Publishing** — no API token is ever stored in the repo.

1. Create an account at https://pypi.org (enable 2FA).
2. Go to **Account settings → Publishing → Add a new pending publisher** and
   enter exactly:
   - PyPI project name: `viper-pm`
   - Owner: `Ramakrishnan3118`
   - Repository name: `viper-pm`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. In the GitHub repo: **Settings → Environments → New environment** named
   `pypi` (no other config needed).

## One-time setup B — apt repository (required for `apt install viper-pm`)

The apt repo is static files on the `gh-pages` branch, signed with a GPG key.

1. Generate a signing key (on any trusted machine):

   ```bash
   gpg --batch --gen-key <<'EOF'
   Key-Type: RSA
   Key-Length: 4096
   Name-Real: viper-pm apt repository
   Name-Email: ramakrishnan@adloggs.com
   Expire-Date: 0
   %no-protection
   EOF
   gpg --list-secret-keys --keyid-format long   # note the key id (after rsa4096/)
   gpg --armor --export-secret-keys <KEYID>     # copy this whole block
   ```

2. In the GitHub repo **Settings → Secrets and variables → Actions**, add:
   - `APT_GPG_PRIVATE_KEY` — the exported private key block
   - `APT_GPG_KEY_ID` — the key id
3. After the first release: **Settings → Pages → Source: Deploy from a
   branch → `gh-pages` / root**.

Keep a backup of the private key somewhere safe; anyone who has it can sign
packages as you.

## Every release

```bash
# 1. bump the version in: pyproject.toml, src/viper_pm/__init__.py, debian/changelog
# 2. commit, then:
git tag v0.1.0
git push origin main --tags
```

The workflow then: runs tests → publishes to PyPI → builds the `.deb` and
attaches it to a GitHub Release → regenerates the signed apt repo on
`gh-pages`.

## What users run

```bash
# pip / pipx (any Linux)
pipx install viper-pm

# apt (Debian/Ubuntu servers) — one-time repo add:
curl -fsSL https://ramakrishnan3118.github.io/viper-pm/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/viper-pm.gpg
echo "deb [signed-by=/usr/share/keyrings/viper-pm.gpg] https://ramakrishnan3118.github.io/viper-pm stable main" \
  | sudo tee /etc/apt/sources.list.d/viper-pm.list
sudo apt update && sudo apt install viper-pm
```

`apt install viper-pm` automatically installs everything it needs —
`python3`, `python3-psutil`, `python3-click`, `python3-rich`, `python3-yaml`
are declared as dependencies in `debian/control`, so a bare server without
Python works out of the box. Upgrades arrive via normal `sudo apt upgrade`.

Manual fallback (no repo add): download the `.deb` from the GitHub Release
page and `sudo apt install ./viper-pm_0.1.0_all.deb` — apt still resolves the
Python dependencies automatically.
