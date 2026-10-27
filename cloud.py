# -*- coding: utf-8 -*-
"""
Cloud runner for the Yonatan Atias alert bot (GitHub Actions).

Each scheduled run (every ~10 min):
  1. answers pending Telegram commands (/start /stop /status)
  2. sends the 10:00 game-day reminder
  3. if a Ramat Gan game is about to start or in progress, stays alive and watches it live
     (polling every 10 s, exactly like the desktop version) until the final buzzer.

Subscriber list + progress are kept in state.enc, encrypted with a key derived from the
bot token (the repo is public, the token lives only in GitHub's encrypted secrets).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bot  # noqa: E402

STATE_ENC = os.path.join(HERE, "state.enc")
CLOUD_SETTINGS = {
    "watch_before_minutes": 60,     # scheduled runs can start a few minutes late
    "poll_seconds": 10,
}


# ------------------------------------------------------------- tiny encryption (stdlib only)
def _keys(token):
    root = hashlib.sha256(b"yonatan-bot-state:" + token.encode()).digest()
    return hashlib.sha256(root + b"enc").digest(), hashlib.sha256(root + b"mac").digest()


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(out[:n])


def encrypt(token, obj):
    ek, mk = _keys(token)
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    tag = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + tag + ct).decode()


def decrypt(token, blob):
    ek, mk = _keys(token)
    raw = base64.b64decode(blob)
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("state.enc doesn't match this bot token")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))))


def load_state(token):
    try:
        with open(STATE_ENC, encoding="utf-8") as f:
            return decrypt(token, f.read().strip())
    except FileNotFoundError:
        return {}
    except Exception as e:
        bot.log(f"couldn't read state.enc ({e}) - starting fresh")
        return {}


def save_state(token, state, previous):
    if state == previous:
        return False
    with open(STATE_ENC, "w", encoding="utf-8") as f:
        f.write(encrypt(token, state) + "\n")
    return True


# ------------------------------------------------------------- run
def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if "--check" in sys.argv:
        ok = True

        def note(good, text):
            nonlocal ok
            ok = ok and good
            print(f"::{'notice' if good else 'error'}::{'OK' if good else 'FAILED'} - {text}")

        try:
            games = bot.team_games(bot.DEFAULTS, bot.http_get_json(bot.SCHEDULE_URL))
            note(bool(games), f"league schedule ({len(games)} Ramat Gan games)")
            past = [g for g in games if (g.get("score_team1") or g.get("score_team2")) and str(g.get("ExternalID", "0")) != "0"]
            if past:
                d = bot.http_get_json(bot.GAME_URL.format(ext=past[-1]["ExternalID"]))
                n = len((d.get("actions") or {}).get("actions") or [])
                note(n > 0, f"live-stats feed (game {past[-1]['ExternalID']}: {n} events)")
            nxt = bot.next_game(games)
            if nxt:
                note(True, f"next game: {nxt.get('team_name_eng_1')} vs {nxt.get('team_name_eng_2')} {nxt.get('game_date_txt')} {nxt.get('game_time')}")
        except Exception as e:
            note(False, f"league site: {e}")
        if token:
            try:
                me = bot.Telegram(token).call("getMe")
                note(True, f"Telegram token (bot @{me['username']})")
            except Exception as e:
                note(False, f"Telegram token: {e}")
        else:
            note(False, "TELEGRAM_TOKEN secret is not set")
        sys.exit(0 if ok else 1)

    if not token:
        print("TELEGRAM_TOKEN secret is missing - add it under Settings > Secrets and variables > Actions.")
        sys.exit(1)

    stored = load_state(token)
    work = tempfile.mkdtemp()
    bot.CONFIG_PATH = os.path.join(work, "config.json")
    bot.STATE_PATH = os.path.join(work, "state.json")
    bot.LOG_PATH = os.path.join(work, "bot.log")

    # seed subscribers on first run (copied from the desktop install)
    chat_ids = stored.get("chat_ids") or [c.strip() for c in os.environ.get("SEED_CHAT_IDS", "").split(",") if c.strip()]
    cfg = dict(bot.DEFAULTS, **CLOUD_SETTINGS, telegram_token=token, chat_ids=chat_ids)
    bot.save_json(bot.CONFIG_PATH, cfg)
    run_state = {"done_games": stored.get("done_games", []), "tg_offset": stored.get("tg_offset", 0),
                 "reminded": stored.get("reminded")}
    bot.save_json(bot.STATE_PATH, run_state)

    b = bot.Bot(cfg)

    def persist():
        new = {"chat_ids": b.subscribers(), "done_games": b.state.get("done_games", []),
               "tg_offset": b.state.get("tg_offset", 0), "reminded": b.state.get("reminded"),
               "day": bot.datetime.now().strftime("%Y-%m-%d")}   # one tiny commit a day keeps the schedule alive
        if save_state(token, new, stored):
            stored.clear()
            stored.update(new)
            bot.log("state saved")

    b.refresh_schedule()            # load the schedule first, so /status knows the next game
    try:
        b.handle_updates(0)
    except Exception as e:
        bot.log(f"telegram updates failed: {e}")

    try:
        b.maybe_remind()
    except Exception as e:
        bot.log(f"reminder error: {e}")
    persist()

    print(f"::notice::subscribers: {len(b.subscribers())} "
          f"({sum(1 for c in b.subscribers() if c.startswith('-'))} group, "
          f"{sum(1 for c in b.subscribers() if not c.startswith('-'))} private)")
    g = bot.game_to_watch(b.cfg, b.games, set(b.state["done_games"]))
    if not g:
        nxt = bot.next_game(b.games)
        bot.log(f"no game right now. next: {nxt.get('team_name_eng_1')} vs {nxt.get('team_name_eng_2')} "
                f"{nxt.get('game_date_txt')} {nxt.get('game_time')}" if nxt else "no game right now.")
        return

    import threading
    threading.Thread(target=b.command_loop, daemon=True).start()   # live /status during the game
    b.watch(g)
    persist()


if __name__ == "__main__":
    main()
