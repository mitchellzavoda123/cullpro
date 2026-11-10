# CullPro

Gameday photo culling for sports photographers. Point it at a card or a folder, and it drops the blurry frames, collapses burst repeats to the sharpest shot, ranks what's left, and has Claude grade every keeper the way a team photo editor would: moment, technical, composition, postability, story, and sponsor value.

Built for my Carolina Gamecock Club Hockey shoots, where a single game is 1,000 to 2,000 frames and the posts need to go up the same night.

## What it does

| Step | What happens | Cost |
|---|---|---|
| 1 · Cull | Flags soft and blurry frames with a contrast-normalized sharpness check | free, runs locally |
| 2 · Dedup | Groups bursts and keeps the sharpest frame of each | free, runs locally |
| 3 · Rank | Scores the survivors on composition, light, color and focus, then Claude grades each one (HERO / KEEPER / MAYBE / REJECT) and reads any sponsor logos in frame | about $1.50 per 600 frames |
| 🧹 Free up space | Moves blurry shots, repeats and Claude rejects to a holding folder, then empties it to get the drive space back | free |
| ⬇ Export | Copies the TOP N and makes 4:5 Instagram crops of the HERO shots | free |

⚡ **God Mode** runs all three steps in one click.

Originals are never edited. Nothing is deleted until you press Empty trash, and Put them all back undoes a cleanup.

## Install (Windows)

1. Install Python from python.org.
2. Download this repo (Code › Download ZIP), unzip it, and double click **Install CullPro.bat**.
3. Open CullPro from the Desktop icon. Paste your Claude API key once under 3 · Rank › Claude AI grading.

Updates: when a new version is out, an **⬆ Update** button shows up in the top bar. One click, a few seconds, no reinstall.

## How the AI grading works

Each surviving frame goes to Claude as a 1024px preview with a fixed rubric (`cullpro_sd.py`) that has a written anchor for every score from 1 to 5, plus hockey and baseball notes. Output is constrained to a JSON schema, the rubric is prompt cached, and every grade is cached on disk so a photo is never paid for twice. Ranking is strict and tier first: HERO, then KEEPER, then MAYBE, with Claude's rejects at the bottom. Inside a tier, Claude's score is blended with the local look score and the slider sets that mix. Then the top 16 HERO and KEEPER frames go to Claude in one side by side request and come back in posting order.

**Your own bar.** Drop 3 to 6 of your best photos into the `references` folder (sidebar › open folder). Claude sees them with every grade as what a 5 looks like. They sit in the prompt cache, so they add well under a cent per frame. Updates never touch that folder.

## Credits

Built on [Photo Curator v7.0](https://github.com/kotyzap/Photo-Curator) by Paolo Cortez (MIT license). The cull, dedup and rank engine and the base interface are his work. CullPro adds the Claude grading, Free up space, Instagram export, Windows app launcher and auto-update.

MIT license. See `LICENSE`.
