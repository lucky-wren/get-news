==========================================================
 newsbrief-lite  -  READ THIS FIRST
==========================================================

DO NOT upload the .zip file on the GitHub web page.

  GitHub "Add file -> Upload files" treats a .zip as ONE binary
  attachment. It will NOT unzip it, will NOT create directories,
  and the workflow / Pages will still not run. Files with
  non-ASCII names also trigger:
  "Something went really wrong, and we can't process that file."

CORRECT WAY (one step):

  1. Unzip this package.
  2. Double-click  deploy.bat
  3. Paste your GitHub Token once, press Enter.
     (Or put the token in token.txt for zero-interaction run.)

  deploy.bat pushes all required files with correct paths via git.
  No login, no repo name, no command line needed.

IF YOU SEE "Something went really wrong" IN A BROWSER:

  - Use the real domain github.com. Mirror / accelerator domains
    are read-only and cannot accept uploads.
  - Use the ASCII-named zip: newsbrief-lite_full_pack_20261123.zip
  - Clear browser cache or use an incognito window.
  - Check githubstatus.com for an ongoing upload incident.
  - Web upload is fine ONLY for a single small text file, created
    via "Add file -> Create new file" (e.g. .github/workflows/daily.yml).

AFTER DEPLOY:

  - Actions -> daily-newsbrief -> Run workflow
  - Settings -> Pages -> Build and deployment -> Source: GitHub Actions

Full details: DELIVERY_NOTES.md  (section 9 = Pages, section 10 = web upload)
==========================================================
