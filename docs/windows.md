# The Windows app

> **The first version is on its way.** Until a release is on the
> [Releases](https://github.com/Solarden/pinkas/releases) page, pinkas runs as a
> [self-hosted](self-hosting.md) server.

For players who don't run a server: one installer, and pinkas runs on the same PC as the game, in its
own window. No accounts, no passwords, no settings files, and nothing leaves your PC.

## What it does

- Find your game folder, or ask you to pick it.
- Install the [addon](addon.md) into the game, and update it when the app updates.
- Read the addon's file whenever the game saves it (at logout or `/reload`), and when the app starts.
- Show every page of pinkas in its window: your characters, search, the charts.
- Let you set everything in the app: routes for the **Route mats** button (pick the alt from a list,
  then `/reload` in the game), profession targets, and your own guide, written in the app.

## Installing

1. Download `pinkas-setup-<version>.exe` from this repository's **Releases** page, and nowhere else. The
   release notes give its SHA-256, if you want to check it. Signed releases use free code signing provided by
   SignPath.io, certificate by SignPath Foundation ([Code signing policy](code-signing.md)).
2. Run it. The first versions are not signed ([Code signing policy](code-signing.md)), so Windows shows
   **"Windows protected your PC"**. Click **More info**, check that the file is the one you downloaded,
   then **Run anyway**.
   - If instead Windows says **Smart App Control blocked** it, that PC runs only signed apps, and there is
     no "Run anyway": wait for a signed release. Don't turn Smart App Control off for pinkas; Windows
     can't turn it back on without a reinstall.
3. It installs for your Windows user only and needs no administrator rights. Start **pinkas** from the
   Start menu.
4. On the first start, open **Settings → Game**, check the game folder, and click **Install addon**.
5. Restart the game, or type `/reload`. Log out once with each character, and they appear.

## Good to know

- **Keep the app open while you play** if you want every session in the charts. Closing the window
  ends the app; when you open it again it reads what the game saved since, but a character that logged
  in and out several times while the app was closed shows only its latest session.
- **Your data** stays in `%LOCALAPPDATA%\pinkas`. Uninstalling keeps it unless you choose otherwise.
- **The window needs Microsoft Edge WebView2**, which Windows 11 has and most Windows 10 PCs have. If
  yours doesn't, pinkas opens in your browser instead and links to Microsoft's installer.
- **Updating** is installing the new version over the old one.
- **If something goes wrong**, `%LOCALAPPDATA%\pinkas\pinkas.log` holds counts and file hashes only,
  never your characters or your account name, so it is safe to send to whoever helps you.
