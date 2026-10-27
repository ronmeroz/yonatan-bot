# -*- coding: utf-8 -*-
"""
Yonatan Atias sub-alert bot
Sends a Telegram message every time Yonatan Atias (Maccabi Ramat Gan, Winner League)
checks in or out of a game, using the league's live-stats feed on basket.co.il.

Run:  python bot.py           (first run walks you through Telegram setup)
      python bot.py --check   (checks the league feed is reachable and shows the next game)
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "bot.log")

SCHEDULE_URL = "https://basket.co.il/pbp/json/games_all.json"
GAME_URL = "https://basket.co.il/livestats/data/game-{ext}.json"

DEFAULTS = {
    "player_id": "1080",              # Yonatan Atias in the league's live stats
    "player_last_name": "אטיאס",       # fallback match if the id ever changes
    "player_display": "יונתן אטיאס",
    "player_short": "יונתן",
    "team_match": ["רמת גן", "Ramat Gan"],
    "team_display": "רמת גן",
    "poll_seconds": 10,
    "watch_before_minutes": 40,       # lineup shows up ~12-17 min before tip-off
    "max_game_hours": 4,
    "reminder_hour": 10,              # game-day reminder at 10:00
}

PERIOD_START_TIMES = {"10:00", "05:00"}
SETTLE_TIMEOUT_SEC = 120
PLAY_TYPES = {"shot", "freeThrow", "foul", "turnover", "rebound", "steal", "block", "jumpBall", "timeout"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------- utilities
def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def http_get_json(url, timeout=20):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}w={int(time.time() * 1000)}",
        headers={"User-Agent": UA, "Accept": "application/json,*/*",
                 "Referer": "https://basket.co.il/", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8-sig"))


# ---------------------------------------------------------------- telegram
class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, params=None, timeout=30):
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(self.base + method, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode())
        if not res.get("ok"):
            raise RuntimeError(res)
        return res["result"]

    def send(self, chat_id, text):
        for attempt in range(3):
            try:
                self.call("sendMessage", {"chat_id": chat_id, "text": text})
                return True
            except Exception as e:
                log(f"telegram send failed ({attempt + 1}/3): {e}")
                time.sleep(3)
        return False


# ---------------------------------------------------------------- formatting
def fmt_clock(qt):
    try:
        m, s = qt.split(":")
        return f"{int(m)}:{s}"
    except Exception:
        return qt or ""


def fmt_period(q):
    q = int(q or 0)
    if q <= 4:
        return f"רבע {q}"
    return "הארכה" if q == 5 else f"הארכה {q - 4}"


def fmt_minutes(mm):
    try:
        m, s = mm.split(":")
        return f"{int(m)}:{s}"
    except Exception:
        return mm or "0:00"


# ---------------------------------------------------------------- game engine
class GameWatcher:
    """Turns successive snapshots of one game's live-stats JSON into alert messages."""

    def __init__(self, cfg, game):
        self.cfg = cfg
        self.game = game                    # schedule entry (from games_all.json)
        self.seen = set()
        self.on_court = False
        self.pending = []                   # player sub events waiting to settle
        self.quarter_started = set()
        self.player_id = None
        self.side = None                    # "home" / "away" for Ramat Gan
        self.started = False
        self.finished = False
        self.roster_checked = False
        self.last = None                    # last snapshot
        self.ft_trips = {}                  # free-throw trips in progress
        self.quarter_started_seen_play = set()   # quarters where real play has happened
        self.lineup_ids = set()             # Ramat Gan's starting five as it's being entered
        self.lineup_announced = False
        self.played = False

    # --- helpers
    def _find_player(self, d):
        gi = d.get("gameInfo", {})
        for side in ("home", "away"):
            for p in (gi.get(side + "Team") or {}).get("players") or []:
                if str(p.get("id")) == str(self.cfg["player_id"]) or \
                        p.get("lastNameLocal", "").strip() == self.cfg["player_last_name"]:
                    return str(p.get("id")), side
        return None, None

    def _rg_side_from_schedule(self):
        m = self.cfg["team_match"]
        n1 = f"{self.game.get('team_name_1', '')} {self.game.get('team_name_eng_1', '')}"
        return "home" if any(x in n1 for x in m) else "away"

    def _is_rg_team(self, team_id):
        if not self.side or not self.last:
            return False
        rg = (self.last.get("gameInfo", {}).get(self.side + "Team") or {}).get("id")
        return rg is not None and str(team_id) == str(rg)

    def opponent_name(self):
        if (self.side or self._rg_side_from_schedule()) == "home":
            return self.game.get("team_name_2", "")
        return self.game.get("team_name_1", "")

    def score_line(self, action_score=None):
        bx = (self.last or {}).get("boxscore", {}).get("gameInfo", {})
        hs, as_ = bx.get("homeScore", "0"), bx.get("awayScore", "0")
        if action_score and "-" in str(action_score):
            as_, hs = str(action_score).split("-", 1)      # feed lists "away-home"
        side = self.side or self._rg_side_from_schedule()
        mine, theirs = (hs, as_) if side == "home" else (as_, hs)
        return f"{self.cfg['team_display']} {mine} - {theirs} {self.opponent_name()}"

    def player_box(self):
        if not self.last or not self.side or not self.player_id:
            return None
        for p in (self.last.get("boxscore", {}).get(self.side + "Team") or {}).get("players") or []:
            if str(p.get("playerId")) == self.player_id:
                return p
        return None

    def stat_line(self, with_assists=False):
        p = self.player_box()
        if not p:
            return ""
        reb = int(p.get("reb_d") or 0) + int(p.get("reb_o") or 0)
        parts = [f"{fmt_minutes(p.get('minutes'))} דק'", f"{p.get('points', 0)} נק'", f"{reb} ריב'"]
        if with_assists:
            parts.append(f"{p.get('ast', 0)} אס'")
        return " · ".join(parts)

    def live_clock(self):
        bx = (self.last or {}).get("boxscore", {}).get("gameInfo", {})
        q = bx.get("currentQuarter") or 0
        t = bx.get("currentQuarterTime") or {}
        return f"{fmt_period(q)} · {int(t.get('m', 0))}:{int(t.get('s', 0)):02d}"

    # --- core
    def process(self, d, silent=False):
        """Feed a fresh snapshot. Returns a list of message strings."""
        self.last = d
        msgs, score_msgs = [], []
        if not self.player_id:
            self.player_id, self.side = self._find_player(d)

        actions = sorted((d.get("actions") or {}).get("actions") or [], key=lambda a: a.get("id", 0))
        for a in actions:
            aid = a.get("id")
            if aid in self.seen:
                continue
            self.seen.add(aid)
            t, prm = a.get("type"), a.get("parameters") or {}
            if t in PLAY_TYPES:
                self.quarter_started_seen_play.add(int(a.get("quarter") or 0))
            if t == "game" and prm.get("type") == "start-of-game":
                self.started = True
            elif t == "quarter" and prm.get("type") == "start-of-quarter":
                self.quarter_started.add(int(a.get("quarter") or 0))
            elif t == "game" and prm.get("type") == "end-of-game":
                self.finished = True
            if t == "substitution" and int(a.get("quarter") or 0) == 1 and a.get("quarterTime") == "10:00" \
                    and 1 not in self.quarter_started_seen_play and self._is_rg_team(a.get("teamId")):
                if prm.get("playerIn"):
                    self.lineup_ids.add(str(a.get("playerId")))
                else:
                    self.lineup_ids.discard(str(a.get("playerId")))
            if t == "substitution" and self.player_id and str(a.get("playerId")) == self.player_id:
                self.pending.append({
                    "in": bool(prm.get("playerIn")),
                    "q": int(a.get("quarter") or 0),
                    "qt": a.get("quarterTime", ""),
                    "since": time.time(),
                    "id": a.get("id", 0),
                })
            elif t == "shot" and self.player_id and str(a.get("playerId")) == self.player_id \
                    and prm.get("made") == "made":
                score_msgs.append((a.get("id", 0), self._shot_msg(a)))
            elif t == "freeThrow" and self.player_id and str(a.get("playerId")) == self.player_id:
                key = a.get("parentActionId") or f"{a.get('quarter')}-{a.get('quarterTime')}"
                trip = self.ft_trips.setdefault(key, {"made": 0, "taken": 0, "since": time.time()})
                trip["taken"] += 1
                trip["made"] += prm.get("made") == "made"
                trip["awarded"] = int(prm.get("freeThrowsAwarded") or 1)
                if prm.get("made") == "made":
                    trip["last"] = a

        if (d.get("boxscore", {}).get("gameInfo", {}) or {}).get("gameFinished"):
            self.finished = True

        timed = self._resolve(force=silent or self.finished) + score_msgs + \
            self._flush_free_throws(force=silent or self.finished)
        msgs += [m for _, m in sorted(timed, key=lambda x: x[0])]   # always in game order

        if not self.lineup_announced:
            if silent or self.quarter_started_seen_play or self.on_court:
                self.lineup_announced = True
            elif self.player_id and len(self.lineup_ids) >= 5 and self.player_id not in self.lineup_ids:
                self.lineup_announced = True
                msgs.append(f"🪑 {self.cfg['player_short']} לא בחמישייה הפותחת נגד {self.opponent_name()}\n"
                            f"אעדכן כשהוא ייכנס")

        if not silent and self.started and not self.roster_checked:
            self.roster_checked = True
            if not self.player_id:
                msgs.append(f"ℹ️ {self.cfg['player_display']} לא מופיע ברשימת השחקנים של המשחק נגד {self.opponent_name()}.")
        return [] if silent else msgs

    def _shot_msg(self, a):
        prm = a.get("parameters") or {}
        if int(prm.get("points") or 2) == 3:
            what = "שלשה! 🎯"
        elif prm.get("type") == "dunk":
            what = "הטבעה! 💥"
        else:
            what = "סל!"
        total = a.get("totalPlayerPoints")
        tail = f"\nסה\"כ במשחק: {total} נק'" if total else ""
        return (f"🏀 {self.cfg['player_short']} קולע {what}\n"
                f"{fmt_period(a.get('quarter'))} · {fmt_clock(a.get('quarterTime'))}\n"
                f"{self.score_line(a.get('score'))}{tail}")

    def _flush_free_throws(self, force=False):
        msgs = []
        for key in list(self.ft_trips):
            trip = self.ft_trips[key]
            done = trip["taken"] >= trip.get("awarded", 1) or force or \
                time.time() - trip["since"] > SETTLE_TIMEOUT_SEC
            if not done:
                continue
            del self.ft_trips[key]
            if trip["made"] == 0:
                continue
            a = trip["last"]
            total = a.get("totalPlayerPoints")
            tail = f"\nסה\"כ במשחק: {total} נק'" if total else ""
            msgs.append((a.get("id", 0), f"🏀 {self.cfg['player_short']} קולע {trip['made']}/{trip['taken']} מהעונשין\n"
                        f"{fmt_period(a.get('quarter'))} · {fmt_clock(a.get('quarterTime'))}\n"
                        f"{self.score_line(a.get('score'))}{tail}"))
        return msgs

    def _is_period_start(self, ev):
        return ev["qt"] == ("10:00" if ev["q"] <= 4 else "05:00")

    def _resolve(self, force=False):
        msgs = []
        while self.pending:
            ev = self.pending[0]
            if self._is_period_start(ev):
                q = ev["q"]
                # Q1 lineup: nothing to cancel out yet, so announce the moment it appears
                settled = q == 1 or q in self.quarter_started or force or \
                    time.time() - ev["since"] > SETTLE_TIMEOUT_SEC or \
                    any(e["q"] != q or not self._is_period_start(e) for e in self.pending[1:])
                if not settled:
                    break
                group = []
                while self.pending and self.pending[0]["q"] == q and self._is_period_start(self.pending[0]):
                    group.append(self.pending.pop(0))
                before = self.on_court
                for e in group:
                    self.on_court = e["in"]
                    self.played = self.played or e["in"]
                if self.on_court != before:
                    msgs.append((group[-1]["id"], self._msg(self.on_court, q, group[-1]["qt"], lineup=True)))
            else:
                self.pending.pop(0)
                if ev["in"] != self.on_court:
                    self.on_court = ev["in"]
                    self.played = self.played or ev["in"]
                    msgs.append((ev["id"], self._msg(ev["in"], ev["q"], ev["qt"])))
        return msgs

    def _msg(self, went_in, q, qt, lineup=False):
        where = f"{fmt_period(q)} · {fmt_clock(qt)}"
        if went_in:
            if lineup and q == 1 and 1 not in self.quarter_started_seen_play:
                return (f"🔥 {self.cfg['player_display']} בחמישייה הפותחת נגד {self.opponent_name()}!\n"
                        f"המשחק עולה בעוד כמה דקות")
            elif lineup:
                head = f"🟢 {self.cfg['player_display']} פותח את {fmt_period(q)}!"
            else:
                head = f"🟢 {self.cfg['player_display']} נכנס למגרש!"
            return f"{head}\n{where}\n{self.score_line()}"
        if lineup and q == 1 and 1 not in self.quarter_started_seen_play:
            return f"↩️ תיקון: {self.cfg['player_short']} בסוף לא בחמישייה הפותחת"
        stats = self.stat_line()
        tail = f"\nעד עכשיו: {stats}" if stats else ""
        return f"⚪ {self.cfg['player_short']} ירד לספסל\n{where}\n{self.score_line()}{tail}"

    def status_text(self):
        state = "על המגרש 🟢" if self.on_court else "על הספסל ⚪"
        s = f"{self.live_clock()}\n{self.score_line()}\n{self.cfg['player_short']} כרגע {state}"
        st = self.stat_line()
        return s + (f"\n{st}" if st else "")

    def final_text(self):
        bx = (self.last or {}).get("boxscore", {}).get("gameInfo", {})
        s = f"🏁 סיום: {self.score_line()}"
        p = self.player_box()
        if p and (p.get("minutes") not in (None, "", "00:00", "0:00")):
            s += f"\n{self.cfg['player_short']}: {self.stat_line(with_assists=True)}"
        elif self.player_id and not self.played:
            s += f"\n{self.cfg['player_short']} לא נכנס הערב."
        return s


# ---------------------------------------------------------------- schedule
def parse_game_time(g):
    try:
        d = datetime.strptime(g["game_date_txt"], "%d/%m/%Y")
        if g.get("game_time"):
            h, m = g["game_time"].split(":")
            return d.replace(hour=int(h), minute=int(m))
        return d.replace(hour=12)
    except Exception:
        return None


def team_games(cfg, schedule):
    out = []
    for block in schedule or []:
        for g in block.get("games") or []:
            names = " ".join(str(g.get(k, "")) for k in
                             ("team_name_1", "team_name_2", "team_name_eng_1", "team_name_eng_2"))
            if any(m in names for m in cfg["team_match"]):
                g["_start"] = parse_game_time(g)
                out.append(g)
    out.sort(key=lambda g: g["_start"] or datetime.max)
    return out


def game_to_watch(cfg, games, done):
    now = datetime.now()
    for g in games:
        ext = str(g.get("ExternalID") or "0")
        if ext == "0" or ext in done or not g["_start"]:
            continue
        start = g["_start"]
        in_window = start - timedelta(minutes=cfg["watch_before_minutes"]) <= now <= \
            start + timedelta(hours=cfg["max_game_hours"])
        live_flag = g.get("isLive") == 0 and start.date() == now.date()
        if in_window or live_flag:
            return g
    return None


def next_game(games):
    now = datetime.now()
    for g in games:
        if g["_start"] and g["_start"] + timedelta(hours=3) > now and not (g.get("score_team1") or g.get("score_team2")):
            return g
    return None


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg["telegram_token"])
        self.state = load_json(STATE_PATH, {"done_games": [], "tg_offset": 0})
        self.watcher = None
        self.games = []
        self.lock = threading.Lock()

    def subscribers(self):
        return list(dict.fromkeys(str(c) for c in self.cfg.get("chat_ids", [])))

    def broadcast(self, text):
        log("SEND: " + text.replace("\n", " | "))
        for cid in self.subscribers():
            self.tg.send(cid, text)

    def save_cfg(self):
        save_json(CONFIG_PATH, self.cfg)

    # --- Telegram commands: /start /stop /status
    def command_loop(self):
        while True:
            try:
                self.handle_updates(50)
            except Exception as e:
                log(f"command loop error: {e}")
                time.sleep(10)

    def handle_updates(self, wait=0):
        """Read pending Telegram messages and answer /start /stop /status."""
        updates = self.tg.call("getUpdates", {"offset": self.state.get("tg_offset", 0), "timeout": wait},
                               timeout=wait + 15)
        for u in updates:
            self.state["tg_offset"] = u["update_id"] + 1
            save_json(STATE_PATH, self.state)
            msg = u.get("message") or {}
            cid = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip().split("@")[0].lower()
            if not cid:
                continue
            if text == "/start":
                if cid not in self.subscribers():
                    self.cfg.setdefault("chat_ids", []).append(cid)
                    self.save_cfg()
                self.tg.send(cid, f"✅ נרשמת! תקבל הודעה בכל פעם ש{self.cfg['player_display']} נכנס או יוצא מהמגרש.\n/status - מצב עכשיו\n/stop - הפסקת התראות")
            elif text == "/stop":
                self.cfg["chat_ids"] = [c for c in self.subscribers() if c != cid]
                self.save_cfg()
                self.tg.send(cid, "🔕 ההתראות הופסקו. /start כדי לחזור.")
            elif text == "/status":
                self.tg.send(cid, self.status_text())

    def status_text(self):
        with self.lock:
            if self.watcher and self.watcher.started and not self.watcher.finished:
                return self.watcher.status_text()
        g = next_game(self.games)
        if not g:
            return "אין משחק קרוב בלוח."
        opp = g["team_name_2"] if any(m in f"{g.get('team_name_1')} {g.get('team_name_eng_1')}" for m in self.cfg["team_match"]) else g["team_name_1"]
        when = g["_start"].strftime("%d/%m %H:%M") if g.get("game_time") else g["game_date_txt"]
        return f"המשחק הבא: נגד {opp}, {when}.\nאעדכן כשהמשחק יתחיל."

    # --- main loop
    def refresh_schedule(self):
        try:
            self.games = team_games(self.cfg, http_get_json(SCHEDULE_URL))
        except Exception as e:
            log(f"schedule fetch failed: {e}")

    def maybe_remind(self):
        """Game-day reminder at reminder_hour (or as soon as the bot is up, if later, before tip-off)."""
        now = datetime.now()
        if now.hour < self.cfg["reminder_hour"]:
            return
        today = now.strftime("%d/%m/%Y")
        if self.state.get("reminded") == today:
            return
        todays = [g for g in self.games if g.get("game_date_txt") == today]
        if not todays:
            return
        self.state["reminded"] = today
        save_json(STATE_PATH, self.state)
        g = todays[0]
        if g.get("game_time") and g["_start"] and g["_start"] <= now:
            return                                  # bot came up after tip-off - live alerts cover it
        home = any(m in f"{g.get('team_name_1')} {g.get('team_name_eng_1')}" for m in self.cfg["team_match"])
        opp = g["team_name_2"] if home else g["team_name_1"]
        when = f"ב-{g['game_time']}" if g.get("game_time") else "(השעה עוד לא פורסמה)"
        lines = [f"📅 היום יש משחק!",
                 f"{self.cfg['team_display']} נגד {opp} {when}",
                 "משחק בית 🏠" if home else "משחק חוץ ✈️"]
        if g.get("liveChannel"):
            lines.append(f"שידור: {g['liveChannel']}")
        lines.append(f"אעדכן ברגע שיהיה ידוע אם {self.cfg['player_short']} בחמישייה (בערך רבע שעה לפני).")
        self.broadcast("\n".join(lines))

    def run(self):
        self.refresh_schedule()         # before answering commands, so /status knows the next game
        last_sched = time.time()
        threading.Thread(target=self.command_loop, daemon=True).start()
        log("bot running")
        while True:
            if time.time() - last_sched > 600:
                self.refresh_schedule()
                last_sched = time.time()
            try:
                self.maybe_remind()
            except Exception as e:
                log(f"reminder error: {e}")
            g = game_to_watch(self.cfg, self.games, set(self.state["done_games"]))
            if g:
                self.watch(g)
                last_sched = 0
            else:
                time.sleep(60)

    def watch(self, g):
        ext = str(g["ExternalID"])
        log(f"watching game {ext}: {g.get('team_name_1')} vs {g.get('team_name_2')} {g.get('game_date_txt')} {g.get('game_time')}")
        w = GameWatcher(self.cfg, g)
        with self.lock:
            self.watcher = w
        first = True
        deadline = (g["_start"] or datetime.now()) + timedelta(hours=self.cfg["max_game_hours"])
        errors = 0
        while datetime.now() < deadline:
            try:
                d = http_get_json(GAME_URL.format(ext=ext))
                errors = 0
            except Exception as e:
                errors += 1
                if errors in (1, 10) or errors % 60 == 0:
                    log(f"game feed not available yet ({errors}): {e}")
                time.sleep(self.cfg["poll_seconds"] if errors < 30 else 30)
                continue

            with self.lock:
                if first:
                    first = False
                    # lineups are entered before tip-off, so only real play means we joined late
                    already_running = any(a.get("type") in PLAY_TYPES for a in
                                          (d.get("actions") or {}).get("actions") or [])
                    if already_running:
                        # joined mid-game: catch up silently, then send one status message
                        w.process(d, silent=True)
                        w.roster_checked = True
                        if not w.finished:
                            self.broadcast(f"🏀 המשחק כבר בעיצומו\n{w.status_text()}")
                    else:
                        msgs = w.process(d)
                        for m in msgs:
                            self.broadcast(m)
                    if w.finished:
                        break
                    time.sleep(self.cfg["poll_seconds"])
                    continue

                was_playing = bool(w.quarter_started_seen_play)
                msgs = w.process(d)
                for m in msgs:
                    self.broadcast(m)
                if w.quarter_started_seen_play and not was_playing and not w.finished:
                    self.broadcast(f"🏀 שריקת פתיחה! {self.cfg['team_display']} נגד {w.opponent_name()}")
                if w.finished:
                    self.broadcast(w.final_text())
                    break
            time.sleep(self.cfg["poll_seconds"])

        self.state["done_games"] = (self.state["done_games"] + [ext])[-200:]
        save_json(STATE_PATH, self.state)
        with self.lock:
            self.watcher = None
        log(f"done with game {ext}")


# ---------------------------------------------------------------- setup
def setup_wizard():
    print("\n=== Yonatan Atias alert bot - first-time setup ===\n")
    print("1. In Telegram, open @BotFather, send /newbot, pick any name.")
    print("2. BotFather replies with a token like 123456789:ABCdef...  Paste it here.\n")
    while True:
        token = input("Bot token: ").strip()
        try:
            me = Telegram(token).call("getMe")
            break
        except Exception:
            print("That token didn't work - copy it again from BotFather.\n")
    tg = Telegram(token)
    print(f"\nGreat - your bot is @{me['username']}.")
    print(f"3. Open https://t.me/{me['username']} on your phone and press START.\n")
    print("Waiting for you to press START ...")
    offset, chat_id = 0, None
    while not chat_id:
        for u in tg.call("getUpdates", {"offset": offset, "timeout": 30}, timeout=45):
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("id"):
                chat_id = str(chat["id"])
    tg.call("getUpdates", {"offset": offset, "timeout": 0})
    cfg = dict(DEFAULTS, telegram_token=token, chat_ids=[chat_id])
    save_json(CONFIG_PATH, cfg)
    save_json(STATE_PATH, {"done_games": [], "tg_offset": offset})
    tg.send(chat_id, f"✅ הבוט מחובר! תקבל הודעה בכל פעם ש{cfg['player_display']} נכנס או יוצא מהמגרש.\n/status - מצב עכשיו\n/stop - הפסקת התראות\n\nחברים יכולים להצטרף: שלח להם את @{me['username']} ושילחצו START.")
    print("Connected! A confirmation message was sent to your Telegram.\n")

    cfg["_background"] = False
    if os.name == "nt":
        ans = input("Start the bot automatically when Windows starts? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes", "כ", "כן"):
            cfg["_background"] = add_to_startup()
    return cfg


def pythonw_path():
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        cand = pyw[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return pyw


def add_to_startup():
    try:
        startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
        pyw = pythonw_path()
        vbs = os.path.join(startup, "YonatanAlertBot.vbs")
        script = os.path.join(HERE, "bot.py")
        with open(vbs, "w", encoding="utf-8") as f:
            f.write(f'CreateObject("WScript.Shell").Run """{pyw}"" ""{script}""", 0, False\n')
        print("Added to Windows startup (runs hidden in the background).")
        return True
    except Exception as e:
        print(f"Couldn't add to startup: {e}")
        return False


def check_feed():
    cfg = dict(DEFAULTS, **load_json(CONFIG_PATH, {}))
    try:
        games = team_games(cfg, http_get_json(SCHEDULE_URL))
    except Exception as e:
        print(f"FAILED to reach the league schedule: {e}")
        return False
    print(f"League schedule OK - {len(games)} Ramat Gan games found.")
    live = game_to_watch(cfg, games, set())
    g = live or next_game(games)
    if g and str(g.get("ExternalID", "0")) != "0":
        try:
            d = http_get_json(GAME_URL.format(ext=g["ExternalID"]))
            print(f"Live-stats feed OK (game {g['ExternalID']}, {len((d.get('actions') or {}).get('actions') or [])} events so far).")
        except Exception as e:
            print(f"Live-stats feed for game {g['ExternalID']} not available yet ({e}) - normal before tip-off.")
    if g:
        print(f"{'LIVE NOW' if live else 'Next game'}: {g.get('team_name_eng_1')} vs {g.get('team_name_eng_2')}, {g.get('game_date_txt')} {g.get('game_time')}")
    return True


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47831))
        return s
    except OSError:
        return None


def main():
    if "--check" in sys.argv:
        check_feed()
        return
    lock = single_instance()
    if not lock:
        print("The bot is already running in the background.")
        return
    cfg = load_json(CONFIG_PATH, None)
    if not cfg or not cfg.get("telegram_token"):
        cfg = setup_wizard()
        check_feed()
        if cfg.pop("_background", False):
            # hand over to a hidden background copy and close this window
            import subprocess
            lock.close()
            subprocess.Popen([pythonw_path(), os.path.abspath(__file__)], cwd=HERE,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            print("\nAll set! The bot is now running in the background - you can close this window.")
            input("Press Enter to close...")
            return
        print("\nBot is running. Leave this window open during games.\n")
    cfg.pop("_background", None)
    cfg = dict(DEFAULTS, **cfg)
    # older installs saved a 20-minute window - make sure we start early enough for the lineup
    cfg["watch_before_minutes"] = max(int(cfg.get("watch_before_minutes") or 0), DEFAULTS["watch_before_minutes"])
    Bot(cfg).run()


if __name__ == "__main__":
    main()
