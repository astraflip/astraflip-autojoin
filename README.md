# AstraFlip AutoJoin

A Windows app that auto-joins giveaways and auto-buys marketplace deals on astraflip.com across several of your own accounts.

**Download only from the [Releases](../../releases) page of this repo.** Never run a copy someone sends you directly.

## Is it safe? Check it yourself

Everything the app does is in [`autojoin_app.py`](autojoin_app.py). The download is not built on anyone's PC: GitHub builds it from this code ([`.github/workflows/build.yml`](.github/workflows/build.yml)) and signs a record of that build.

To check your download:

1. **Build record (strongest).** With the [GitHub CLI](https://cli.github.com) installed, run:
   ```
   gh attestation verify "AstraFlip AutoJoin.exe" -R astraflip/astraflip-autojoin
   ```
   It passes only if GitHub built that exact file from this repo.
2. **Fingerprint.** In PowerShell, run `Get-FileHash "AstraFlip AutoJoin.exe"` and compare it with `SHA256.txt` on the release.
3. **Read the build log.** Each release links to the GitHub Actions run that made it.

Some antivirus tools flag apps packed with PyInstaller even when they are clean. That is a known false alarm. The checks above are how you tell.

## What it stores

Everything stays in the folder next to the .exe: your settings, your account logins (one browser profile per account) and your stats. Nothing is uploaded.

## Who it talks to

| Address | Why |
|---|---|
| astraflip.com | The site itself, in your own logged-in browser windows |
| thumbnails.roblox.com, api.rolimons.com | Item pictures and values |
| astraflip-stats … workers.dev | Optional community stats (just total entered and spent) and the new-giveaway feed. Turn off in Settings. |
| mempool.space, eth.blockscout.com, api.mainnet-beta.solana.com, litecoinspace.org, api.coingecko.com, api.coinbase.com | Showing total tips on the Tip tab (public blockchain data and prices) |
| github.com | Checking for updates |
| discord.com | Only if you add your own Discord webhook for alerts |

It never asks for or sends your password; you log in to the site yourself in the windows it opens.

## Build it yourself

```
pip install pyinstaller playwright
python -m PyInstaller --noconfirm --onefile --noconsole --icon icon.ico --name "AstraFlip AutoJoin" --collect-all playwright autojoin_app.py
```
