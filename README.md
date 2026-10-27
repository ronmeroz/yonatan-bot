# Yonatan Atias alert bot 🏀

Telegram alerts for Maccabi Ramat Gan games (Winner League):
starting five, every check-in / check-out, every basket, final stats, and a 10:00 game-day reminder.

Runs on GitHub Actions every 10 minutes; during a game it polls the league's live stats every 10 seconds.

- `cloud.py` – the scheduled runner
- `bot.py` – alert logic (same as the desktop version)
- `state.enc` – encrypted subscriber list / progress (key derived from the bot token secret)

Secret needed: `TELEGRAM_TOKEN` (Settings → Secrets and variables → Actions).
