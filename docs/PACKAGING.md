# How viper-pm becomes MIT-licensed and apt-installable

## 1. MIT license (one-time, 5 minutes)

1. Create `LICENSE` at the repo root with the standard MIT text:

   ```
   MIT License

   Copyright (c) 2026 Adloggs / <your names>

   Permission is hereby granted, free of charge, to any person obtaining a copy
   of this software and associated documentation files (the "Software"), to deal
   in the Software without restriction, including without limitation the rights
   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   copies of the Software, and to permit persons to whom the Software is
   furnished to do so, subject to the following conditions:

   The above copyright notice and this permission notice shall be included in
   all copies or substantial portions of the Software.

   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
   SOFTWARE.
   ```

2. In `pyproject.toml`: `license = "MIT"` (SPDX id) and include the file.
3. Debian packaging additionally wants `debian/copyright` in machine-readable
   (DEP-5) format — same MIT text, per-file copyright holders.

That's it. MIT is GPL-compatible and Debian-acceptable (DFSG-free), so it
blocks nothing below.

## 2. Two routes to `apt install`

### Route A — your own apt repository (do this first; days, not months)

This is what Docker, Node.js, GitHub CLI etc. do. Users add one repo line, then
`apt install viper-pm` and get upgrades via normal `apt upgrade`.

**Step 1 — build a `.deb`.** Add a `debian/` directory:

```
debian/
├── control        # package name, deps (python3, python3-psutil...), description
├── rules          # #!/usr/bin/make -f  →  %:  dh $@ --with python3 --buildsystem=pybuild
├── changelog      # version history (dch tool maintains it)
├── copyright      # DEP-5 MIT copyright
├── viper-pm.service   # systemd unit installed with the package
└── source/format  # 3.0 (native) while we self-host
```

Build locally: `dpkg-buildpackage -us -uc -b` → produces `viper-pm_0.1.0_all.deb`
(pure Python → arch `all`, one deb works on amd64/arm64).
Sanity check with `lintian` and `sudo apt install ./viper-pm_0.1.0_all.deb`.

**Step 2 — sign.** Create a GPG key for the project
(`gpg --full-generate-key`, RSA 4096, no expiry or long expiry). The repo
metadata gets signed with it; users trust that key once.

**Step 3 — host the repo.** Easiest modern options:

- **aptly** or **reprepro** to generate the pool/dists structure, pushed to
  **GitHub Pages** (free, HTTPS, fine for a repo this size), or
- a paid-free hybrid like Cloudsmith/PackageCloud (free tiers for OSS).

**Step 4 — users install:**

```bash
curl -fsSL https://ramakrishnan3118.github.io/viper-pm/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/viper-pm.gpg
echo "deb [signed-by=/usr/share/keyrings/viper-pm.gpg] \
  https://ramakrishnan3118.github.io/viper-pm stable main" \
  | sudo tee /etc/apt/sources.list.d/viper-pm.list
sudo apt update && sudo apt install viper-pm
```

Ship those four lines as a one-shot `install.sh` for convenience.

**Step 5 — automate.** GitHub Actions on every tagged release: build deb →
sign → publish to the Pages repo. Releases become `git tag v0.2.0 && git push --tags`.

**Ubuntu-only alternative:** a **Launchpad PPA** (free) — upload source
package, Launchpad builds and hosts it; users do
`sudo add-apt-repository ppa:ramakrishnan3118/viper-pm && apt install viper-pm`.
Slightly more ceremony (source-only uploads, per-Ubuntu-series builds) but
zero hosting to maintain. We can do both.

### Route B — official Debian/Ubuntu archives (later, optional)

`sudo apt install viper-pm` with *no* added repo requires acceptance into
Debian proper: file an ITP bug, meet Debian Policy, find a Debian Developer
sponsor to upload; it then flows into Ubuntu automatically. Realistic once the
project is stable and has users — typically months. Route A serves everyone
meanwhile and forever.

## 3. Snap (parallel channel)

`snapcraft.yaml` + Snap Store account. Because a process manager spawns
arbitrary user commands, it needs **classic confinement**, which requires a
manual one-time review by the Snap Store team (routinely granted for dev
tools — this is why `snap if can`). PyPI/pipx (`pipx install viper-pm`)
covers any machine where neither apt repo nor snap is wanted.

## 4. Order of operations

1. `LICENSE` (MIT) + `pyproject.toml` — day one, before any code matters.
2. Working package on PyPI (cheapest channel, proves the wheel).
3. `debian/` dir + local deb build + lintian clean.
4. GPG key + GitHub Pages apt repo + CI publishing → **apt install achieved.**
5. Launchpad PPA (optional convenience for Ubuntu users).
6. Snap classic-confinement request.
7. (Much later, if wanted) Debian ITP for the official archive.
