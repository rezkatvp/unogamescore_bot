import asyncio
import json
import logging
import os
import random
import string
from pathlib import Path
from typing import Dict, Any, Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove,
)

# ---------------- CONFIG ----------------

TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = Path("uno_games.json")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("uno")

router = Router()

# ---------------- STORAGE ----------------

def generate_game_code() -> str:
    """Генерує унікальний код гри (4 символи)"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.games_by_code: Dict[str, Dict[str, Any]] = {}  # code -> game
        self.user_games: Dict[str, str] = {}               # user_id -> code
        self.load()

    def load(self):
        if not self.path.exists():
            self.games_by_code = {}
            self.user_games = {}
            log.info("Loaded games: 0")
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if "games_by_code" in raw:
                self.games_by_code = raw["games_by_code"]
                self.user_games = raw.get("user_games", {})
            else:
                # старий/невідомий формат
                self.games_by_code = {}
                self.user_games = {}
            log.info("Loaded games: %d", len(self.games_by_code))
        except Exception as e:
            log.error("Load error: %s", e)
            self.games_by_code = {}
            self.user_games = {}

    def save(self):
        try:
            self.path.write_text(
                json.dumps(
                    {"games_by_code": self.games_by_code, "user_games": self.user_games},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            log.error("Save error: %s", e)

    def create_game(self, creator_uid: str) -> str:
        """Створює нову гру і повертає код"""
        code = generate_game_code()
        while code in self.games_by_code:
            code = generate_game_code()

        self.games_by_code[code] = {
            "status": "awaiting_players",
            "created_by": creator_uid,
            "players": {},
            "scores": {},
            "wins": {},
            "total_points": {},
            "target": None,
            "mode": None,
            "rounds": [],
            "active_round": None,
            "awaiting_custom_target_from": None,

            # UX: коли людина натиснула "Приєднатись" з меню
            "pending_join_from": None,
        }
        self.user_games[creator_uid] = code
        self.save()
        return code

    def get_user_game(self, user_id: str) -> Optional[Dict[str, Any]]:
        code = self.user_games.get(user_id)
        if code:
            return self.games_by_code.get(code)
        return None

    def get_user_game_code(self, user_id: str) -> Optional[str]:
        return self.user_games.get(user_id)

    def join_game(self, user_id: str, code: str) -> bool:
        code = code.upper()
        if code not in self.games_by_code:
            return False
        game = self.games_by_code[code]
        if game["status"] != "awaiting_players":
            return False
        if user_id in game["players"]:
            self.user_games[user_id] = code
            self.save()
            return True
        self.user_games[user_id] = code
        self.save()
        return True

    def leave_game(self, user_id: str):
        code = self.user_games.pop(user_id, None)
        if code and code in self.games_by_code:
            game = self.games_by_code[code]
            # прибрати з гравців
            for key in ("players", "scores", "wins", "total_points"):
                if key in game and user_id in game[key]:
                    del game[key][user_id]
            self.save()

store = Store(DATA_FILE)

# ---------------- UI ----------------

def ikb_start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Створити гру", callback_data="start:new")],
        [InlineKeyboardButton(text="🔑 Приєднатись по коду", callback_data="start:join")],
        [
            InlineKeyboardButton(text="📊 Рахунок", callback_data="start:score"),
            InlineKeyboardButton(text="🎴 Новий раунд", callback_data="start:round"),
        ],
        [
            InlineKeyboardButton(text="📜 Історія", callback_data="start:history"),
            InlineKeyboardButton(text="🏆 Топ", callback_data="start:top"),
        ],
        [InlineKeyboardButton(text="↩️ Undo", callback_data="start:undo")],
        [InlineKeyboardButton(text="🏳️ Вийти з гри", callback_data="start:leave")],
        [InlineKeyboardButton(text="❓ Допомога", callback_data="start:help")],
    ])

START_TEXT = (
    "Я — <b>UNO Score</b>, бот, який чесно рахуватиме всі ваші штрафні бали, щоб ніхто не зміг схитрувати!\n\n"
    "🚀 <b>Швидкий старт:</b>\n"
    "• Створи гру і отримай код\n"
    "• Друзі приєднаються по коду\n\n"
    "🏆 <b>В процесі гри:</b>\n"
    "• Рахунок, історія, топ\n"
    "• Undo останнього раунду\n"
    "• Вихід з гри\n\n"
    "Забудь про папір та ручку. Тисни кнопку і погнали 🎴"
)

def user_name(u) -> str:
    full = (getattr(u, "full_name", "") or "").strip()
    if full:
        return full
    username = getattr(u, "username", None)
    if username:
        return f"@{username}"
    return f"User{u.id}"

async def notify_all_players(bot: Bot, game: Dict[str, Any], message: str, exclude_uid: Optional[str] = None):
    for uid in list(game.get("players", {}).keys()):
        if exclude_uid and uid == exclude_uid:
            continue
        try:
            await bot.send_message(chat_id=int(uid), text=message, parse_mode="HTML")
        except Exception as e:
            log.warning("Не вдалося відправити повідомлення гравцю %s: %s", uid, e)

def players_text(game: Dict[str, Any]) -> str:
    if not game["players"]:
        return "Поки що ніхто не приєднався."
    return "\n".join(f"• {p['name']}" for p in game["players"].values())

def score_text(game: Dict[str, Any], show_stats: bool = False) -> str:
    if not game.get("target"):
        return "Ліміт ще не вибрано."
    target = int(game["target"])
    players = game["players"]
    scores = game["scores"]
    wins = game.get("wins", {})
    total_points = game.get("total_points", {})
    rounds_count = len(game.get("rounds", []))

    if not players:
        return "Немає гравців."

    leader_uid = min(scores, key=lambda k: int(scores[k])) if scores else None

    lines = []
    for uid, p in players.items():
        s = int(scores.get(uid, 0))
        tag = ""
        if s >= target:
            tag = " 💀"
        elif s >= int(target * 0.75):
            tag = " 🔥"
        elif uid == leader_uid:
            tag = " 👑"

        progress = f"{s}/{target}"
        if show_stats:
            win_count = wins.get(uid, 0)
            avg = round(total_points.get(uid, 0) / rounds_count, 1) if rounds_count > 0 else 0
            lines.append(f"<b>{p['name']}</b>: {progress}{tag} | 🏆{win_count} | 📊{avg}")
        else:
            lines.append(f"<b>{p['name']}</b>: {progress}{tag}")

    return "\n".join(lines)

def game_over_text(game: Dict[str, Any]) -> Optional[str]:
    if not game.get("target"):
        return None
    target = int(game["target"])
    scores = {uid: int(v) for uid, v in game["scores"].items()}
    if not scores:
        return None
    if not any(v >= target for v in scores.values()):
        return None

    ranking = sorted(scores.items(), key=lambda x: x[1])
    winner_uid = ranking[0][0]
    winner_name = game["players"].get(winner_uid, {}).get("name", winner_uid)

    out = [
        "🏁 <b>ГРА ЗАКІНЧИЛАСЯ!</b>\nХтось набрав або перевищив ліміт.\n",
        "<b>Фінальний рейтинг (менше = краще):</b>",
    ]
    for i, (uid, sc) in enumerate(ranking, start=1):
        nm = game["players"].get(uid, {}).get("name", uid)
        out.append(f"{i}. {nm} — {sc}")
    out.append(f"\n🎉 <b>Переможець:</b> {winner_name}")
    return "\n".join(out)

# ---------------- ROUND CORE ----------------

def begin_round(game: Dict[str, Any], leader_uid: str):
    order = list(game["players"].keys())
    game["active_round"] = {
        "order": order,
        "pos": 0,
        "leader_uid": leader_uid,
        "delta": {uid: None for uid in order},
        "awaiting_from": None,  # для "each"
    }

def apply_round(game: Dict[str, Any]):
    ar = game["active_round"]
    delta = ar["delta"]
    for uid, pts in delta.items():
        pts_int = int(pts)
        game["scores"][uid] = int(game["scores"].get(uid, 0)) + pts_int
        game.setdefault("total_points", {})[uid] = game["total_points"].get(uid, 0) + pts_int
        if pts_int == 0:
            game.setdefault("wins", {})[uid] = game["wins"].get(uid, 0) + 1

    game.setdefault("rounds", []).append({"delta": delta.copy()})
    game["active_round"] = None

def undo_round(game: Dict[str, Any]) -> bool:
    rounds = game.get("rounds", [])
    if not rounds:
        return False
    last = rounds.pop()
    delta = last["delta"]
    for uid, pts in delta.items():
        pts_int = int(pts)
        game["scores"][uid] = max(0, int(game["scores"].get(uid, 0)) - pts_int)
        if uid in game.get("total_points", {}):
            game["total_points"][uid] = max(0, game["total_points"][uid] - pts_int)
        if pts_int == 0 and uid in game.get("wins", {}):
            game["wins"][uid] = max(0, game["wins"].get(uid, 0) - 1)
    return True

def all_filled(game: Dict[str, Any]) -> bool:
    ar = game.get("active_round")
    if not ar:
        return False
    return all(v is not None for v in ar["delta"].values())

# ---------------- PURE ACTIONS (for callbacks) ----------------

async def action_new_game(bot: Bot, chat_id: int, user) -> None:
    creator_uid = str(user.id)

    existing_game = store.get_user_game(creator_uid)
    if existing_game and existing_game.get("status") != "finished":
        await bot.send_message(chat_id, "У тебе вже є активна гра. Натисни «🏳️ Вийти з гри» щоб вийти.")
        return

    code = store.create_game(creator_uid)
    game = store.games_by_code[code]

    nm = user_name(user)
    game["players"][creator_uid] = {"name": nm}
    game["scores"][creator_uid] = 0
    game["wins"][creator_uid] = 0
    game["total_points"][creator_uid] = 0
    store.save()

    await bot.send_message(
        chat_id,
        f"🎮 <b>Нова гра UNO створена!</b>\n\n"
        f"Код гри: <code>{code}</code>\n\n"
        f"Поділись кодом з друзями.\n"
        f"Вони мають приєднатись командою: <code>/join {code}</code>\n\n"
        f"Коли всі зібрались, напиши: <b>/start_game</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

async def action_score(bot: Bot, chat_id: int, user_id: str) -> None:
    game = store.get_user_game(user_id)
    if not game:
        await bot.send_message(chat_id, "Ти не в грі. Натисни «🎮 Створити гру» або введи /join КОД.")
        return
    if game.get("status") not in {"running", "finished"}:
        await bot.send_message(chat_id, "Гра ще не запущена. Хост має натиснути /start_game.")
        return
    await bot.send_message(chat_id, score_text(game, show_stats=True), parse_mode="HTML")

async def action_history(bot: Bot, chat_id: int, user_id: str) -> None:
    game = store.get_user_game(user_id)
    if not game or game.get("status") not in {"running", "finished"}:
        await bot.send_message(chat_id, "Гра ще не запущена.")
        return

    rounds = game.get("rounds", [])
    if not rounds:
        await bot.send_message(chat_id, "Поки що немає завершених раундів.")
        return

    recent_rounds = rounds[-10:]
    lines = ["📜 <b>Історія раундів</b> (останні 10):\n"]
    for i, rnd in enumerate(recent_rounds, start=len(rounds) - len(recent_rounds) + 1):
        delta = rnd.get("delta", {})
        round_lines = [f"<b>Раунд {i}:</b>"]
        for uid, pts in delta.items():
            name = game["players"].get(uid, {}).get("name", uid)
            if int(pts) == 0:
                round_lines.append(f"  {name}: <b>0</b> 🏆")
            else:
                round_lines.append(f"  {name}: +{pts}")
        lines.append("\n".join(round_lines))

    await bot.send_message(chat_id, "\n\n".join(lines), parse_mode="HTML")

async def action_top(bot: Bot, chat_id: int, user_id: str) -> None:
    game = store.get_user_game(user_id)
    if not game or game.get("status") not in {"running", "finished"}:
        await bot.send_message(chat_id, "Гра ще не запущена.")
        return

    players = game["players"]
    scores = game["scores"]
    wins = game.get("wins", {})
    rounds_count = len(game.get("rounds", []))
    total_points = game.get("total_points", {})

    if not players:
        await bot.send_message(chat_id, "Немає гравців.")
        return

    top_wins = sorted(
        [(uid, wins.get(uid, 0), players[uid]["name"]) for uid in players.keys()],
        key=lambda x: x[1],
        reverse=True
    )
    top_scores = sorted(
        [(uid, int(scores.get(uid, 0)), players[uid]["name"]) for uid in players.keys()],
        key=lambda x: x[1]
    )

    lines = ["🏆 <b>Топ гравців</b>\n", "<b>За перемогами:</b>"]
    for i, (_uid, win_count, name) in enumerate(top_wins[:3], start=1):
        medal = ["🥇", "🥈", "🥉"][i - 1]
        lines.append(f"{medal} {name}: {win_count} перемог")

    lines.append("\n<b>За найменшими балами:</b>")
    for i, (_uid, scorev, name) in enumerate(top_scores[:3], start=1):
        medal = ["🥇", "🥈", "🥉"][i - 1]
        avg = round(total_points.get(_uid, 0) / rounds_count, 1) if rounds_count > 0 else 0
        lines.append(f"{medal} {name}: {scorev} балів (середнє: {avg})")

    await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

async def action_undo(bot: Bot, chat_id: int, user_id: str) -> None:
    game = store.get_user_game(user_id)
    if not game:
        await bot.send_message(chat_id, "Ти не в грі.")
        return
    if game.get("active_round"):
        await bot.send_message(chat_id, "Зараз активний раунд. Завершіть його.")
        return
    if not undo_round(game):
        await bot.send_message(chat_id, "Немає що відкотити.")
        return
    store.save()
    msg = "↩️ Останній раунд скасовано.\n\n" + score_text(game, show_stats=True)
    await bot.send_message(chat_id, msg, parse_mode="HTML")
    await notify_all_players(bot, game, msg, exclude_uid=user_id)

async def action_leave(bot: Bot, chat_id: int, user) -> None:
    uid = str(user.id)
    game = store.get_user_game(uid)
    if not game:
        await bot.send_message(chat_id, "Ти не в грі.")
        return

    code = store.user_games.get(uid)
    store.leave_game(uid)
    await bot.send_message(chat_id, "✅ Ти вийшов з гри.")

    # сповістити інших
    if code:
        g = store.games_by_code.get(code)
        if g:
            player_name = user_name(user)
            for player_uid in g.get("players", {}).keys():
                if player_uid != uid:
                    try:
                        await bot.send_message(int(player_uid), f"ℹ️ <b>{player_name}</b> вийшов з гри.", parse_mode="HTML")
                    except Exception:
                        pass

# ---------------- START / HELP ----------------

@router.message(CommandStart())
async def start(message: Message):
    await message.answer(START_TEXT, reply_markup=ikb_start_menu())

@router.callback_query(F.data.startswith("start:"))
async def start_menu_router(cb: CallbackQuery):
    action = cb.data.split(":", 1)[1]
    await cb.answer()

    uid = str(cb.from_user.id)
    chat_id = cb.message.chat.id
    bot = cb.bot

    if action == "new":
        await action_new_game(bot, chat_id, cb.from_user)
        return

    if action == "join":
        # ставимо прапорець "чекаю код"
        game = store.get_user_game(uid)
        if game:
            game["pending_join_from"] = uid
            store.save()
        await bot.send_message(chat_id, "Введи код гри (4 символи), наприклад: <code>A1B2</code>", parse_mode="HTML")
        return

    if action == "score":
        await action_score(bot, chat_id, uid)
        return

    if action == "history":
        await action_history(bot, chat_id, uid)
        return

    if action == "top":
        await action_top(bot, chat_id, uid)
        return

    if action == "undo":
        await action_undo(bot, chat_id, uid)
        return

    if action == "leave":
        await action_leave(bot, chat_id, cb.from_user)
        return

    if action == "round":
        await bot.send_message(chat_id, "Щоб почати раунд, напиши: <b>/round</b>", parse_mode="HTML")
        return

    if action == "help":
        await bot.send_message(
            chat_id,
            "❓ <b>Як це працює</b>\n"
            "1) Хост створює гру\n"
            "2) Друзі приєднуються по коду\n"
            "3) Хост запускає /start_game і вибирає ліміт та режим\n"
            "4) /round для нового раунду\n\n"
            "Порада: усе працює в приватних чатах з ботом.",
            parse_mode="HTML",
        )
        return

# ---------------- COMMANDS THAT STILL MATTER ----------------

@router.message(Command("new"))
async def cmd_new(message: Message):
    await action_new_game(message.bot, message.chat.id, message.from_user)

@router.message(Command("leave"))
async def cmd_leave(message: Message):
    await action_leave(message.bot, message.chat.id, message.from_user)

@router.message(Command("score"))
async def cmd_score(message: Message):
    await action_score(message.bot, message.chat.id, str(message.from_user.id))

@router.message(Command("history"))
async def cmd_history(message: Message):
    await action_history(message.bot, message.chat.id, str(message.from_user.id))

@router.message(Command("top"))
async def cmd_top(message: Message):
    await action_top(message.bot, message.chat.id, str(message.from_user.id))

@router.message(Command("undo"))
async def cmd_undo(message: Message):
    await action_undo(message.bot, message.chat.id, str(message.from_user.id))

@router.message(Command("join"))
async def join_game(message: Message):
    uid = str(message.from_user.id)
    parts = (message.text or "").split()

    if len(parts) < 2:
        await message.answer("Використання: <code>/join A1B2</code>", parse_mode="HTML")
        return

    code = parts[1].upper()

    existing_code = store.user_games.get(uid)
    if existing_code and existing_code != code:
        await message.answer(f"Ти вже в іншій грі ({existing_code}). Натисни «🏳️ Вийти з гри» щоб вийти.")
        return

    if not store.join_game(uid, code):
        await message.answer("❌ Неправильний код або гра вже почалась.")
        return

    game = store.games_by_code[code]
    nm = user_name(message.from_user)

    if uid not in game["players"]:
        game["players"][uid] = {"name": nm}
        game["scores"][uid] = 0
        game["wins"][uid] = 0
        game["total_points"][uid] = 0
        store.save()

    creator_uid = game["created_by"]
    try:
        await message.bot.send_message(
            chat_id=int(creator_uid),
            text=f"✅ <b>{nm}</b> приєднався до гри!\n\nГравці:\n{players_text(game)}",
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("Не вдалося повідомити організатора: %s", e)

    await message.answer(
        f"✅ Ти приєднався до гри <code>{code}</code>!\n\n"
        f"Гравці:\n{players_text(game)}\n\n"
        f"Очікуй поки організатор запустить гру.",
        parse_mode="HTML",
    )

@router.message(Command("start_game"))
async def start_game(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)

    if not game:
        await message.answer("Ти не в грі.")
        return
    if game["created_by"] != uid:
        await message.answer("Тільки організатор може запустити гру.")
        return
    if game["status"] != "awaiting_players":
        await message.answer(f"Гра вже запущена або не готова. Статус: {game.get('status')}")
        return
    if len(game["players"]) < 2:
        await message.answer("Потрібно мінімум 2 гравці.")
        return

    game["status"] = "setup_target"
    store.save()

    await message.answer(
        "🎯 <b>Оберіть ліміт балів</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="200", callback_data="target:200"),
                InlineKeyboardButton(text="500", callback_data="target:500"),
                InlineKeyboardButton(text="1000", callback_data="target:1000"),
            ],
            [InlineKeyboardButton(text="Інше", callback_data="target:other")],
        ])
    )

@router.callback_query(F.data.startswith("target:"))
async def target_pick(cb: CallbackQuery):
    uid = str(cb.from_user.id)
    game = store.get_user_game(uid)

    if not game:
        await cb.answer("Гра не знайдена.", show_alert=True)
        return
    if game.get("status") != "setup_target":
        await cb.answer(f"Зараз не етап ліміту. Статус: {game.get('status')}", show_alert=True)
        return
    if uid != game["created_by"]:
        await cb.answer("Тільки організатор.", show_alert=True)
        return

    action = cb.data.split(":", 1)[1]

    if action == "other":
        game["awaiting_custom_target_from"] = uid
        store.save()
        await cb.answer()
        await cb.message.answer("Введіть число ліміту (наприклад 300 або 750).", reply_markup=ReplyKeyboardRemove())
        return

    game["target"] = int(action)
    game["awaiting_custom_target_from"] = None
    game["status"] = "setup_mode"
    store.save()

    await cb.answer()
    await cb.message.edit_text(
        "⚙️ <b>Оберіть режим введення балів</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ведучий вводить за всіх", callback_data="mode:leader")],
            [InlineKeyboardButton(text="👥 Кожен вводить сам", callback_data="mode:each")],
        ])
    )

@router.callback_query(F.data.startswith("mode:"))
async def mode_pick(cb: CallbackQuery):
    uid = str(cb.from_user.id)
    game = store.get_user_game(uid)

    if not game or game.get("status") != "setup_mode":
        await cb.answer("Зараз не етап режиму.", show_alert=True)
        return
    if uid != game["created_by"]:
        await cb.answer("Тільки організатор.", show_alert=True)
        return

    action = cb.data.split(":", 1)[1]
    if action not in {"leader", "each"}:
        await cb.answer("Невідомий режим.", show_alert=True)
        return

    game["mode"] = action
    game["status"] = "running"
    store.save()

    code = store.get_user_game_code(uid)
    mode_text = "ведучий вводить за всіх" if action == "leader" else "кожен вводить сам"

    await cb.answer()
    await cb.message.edit_text(
        f"✅ <b>Гру запущено!</b>\n\n"
        f"Код: <code>{code}</code>\n\n"
        f"Гравці:\n{players_text(game)}\n\n"
        f"Ліміт: <b>{game['target']}</b>\n"
        f"Режим: <b>{mode_text}</b>\n\n"
        f"Напиши /round щоб почати раунд",
        parse_mode="HTML",
    )

    await notify_all_players(
        cb.bot, game,
        f"✅ <b>Гра запущена!</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Ліміт: <b>{game['target']}</b>\n"
        f"Режим: <b>{mode_text}</b>\n\n"
        f"Напиши /round щоб почати раунд",
        exclude_uid=uid
    )

@router.message(Command("round"))
async def round_start(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)

    if not game:
        await message.answer("Ти не в грі. Натисни «🎮 Створити гру» або введи /join КОД.")
        return
    if game.get("status") == "finished":
        await message.answer("Гра вже завершена. Створи нову: /new")
        return
    if game.get("status") != "running":
        await message.answer("Гра не запущена. Хост має натиснути /start_game.")
        return
    if game.get("active_round"):
        await message.answer("Раунд вже активний. Введіть бали.")
        return
    if len(game["players"]) < 2:
        await message.answer("Потрібно мінімум 2 гравця.")
        return

    leader_uid = uid
    begin_round(game, leader_uid=leader_uid)
    store.save()

    ar = game["active_round"]
    first_uid = ar["order"][0]
    first_name = game["players"][first_uid]["name"]

    if game["mode"] == "leader":
        await message.answer(f"✍️ Введи бали для <b>{first_name}</b>:", parse_mode="HTML")
    else:
        score_text_msg = "🎴 <b>Раунд старт!</b>\n\n" + score_text(game, show_stats=True)
        await notify_all_players(message.bot, game, score_text_msg)

        if ar["order"]:
            first_uid = ar["order"][0]
            ar["awaiting_from"] = first_uid
            store.save()

            await message.bot.send_message(
                chat_id=int(first_uid),
                text=f"<b>{game['players'][first_uid]['name']}</b>, введи свої бали числом (0 якщо виграв):",
                parse_mode="HTML",
            )

# ---------------- TEXT INPUT ROUTER ----------------

@router.message()
async def text_router(message: Message):
    """
    Обробляємо:
    - введення коду після кнопки "Приєднатись"
    - введення кастомного ліміту
    - введення балів (numeric_router логіка)
    """
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    uid = str(message.from_user.id)
    game = store.get_user_game(uid)

    # 0) Якщо натиснули "Приєднатись" з меню і вводять код
    if text and len(text) == 4 and text.upper().isalnum():
        # дозволимо це тільки якщо людина НЕ в грі, або якщо вона очікує join
        if not game:
            # спробуємо приєднати як /join
            message.text = f"/join {text.upper()}"
            await join_game(message)
            return

    # якщо нема гри, нема що робити
    if not game:
        return

    # 1) custom target
    if game.get("awaiting_custom_target_from") == uid:
        try:
            v = int(text)
            if v < 100:
                await message.answer("Мінімум 100. Спробуй ще раз.")
                return
            game["target"] = v
            game["awaiting_custom_target_from"] = None
            game["status"] = "setup_mode"
            store.save()
            await message.answer(
                "⚙️ <b>Оберіть режим введення балів</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✍️ Ведучий вводить за всіх", callback_data="mode:leader")],
                    [InlineKeyboardButton(text="👥 Кожен вводить сам", callback_data="mode:each")],
                ])
            )
        except ValueError:
            await message.answer("Введи ціле число. Наприклад: 500")
        return

    # 2) points input (only if active_round exists) + only numbers
    ar = game.get("active_round")
    if not ar:
        return

    try:
        pts = int(text)
        if pts < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи невід’ємне ціле число (0, 15, 60...).")
        return

    # Leader mode: only leader can input
    if game.get("mode") == "leader":
        if uid != ar.get("leader_uid"):
            return

        pos = int(ar["pos"])
        order = ar["order"]
        if pos >= len(order):
            return

        for_uid = order[pos]
        ar["delta"][for_uid] = pts
        ar["pos"] = pos + 1
        store.save()

        if ar["pos"] < len(order):
            nxt_uid = order[ar["pos"]]
            nxt_name = game["players"].get(nxt_uid, {}).get("name", "Гравець")
            await message.answer(f"✍️ Введи бали для <b>{nxt_name}</b>:", parse_mode="HTML")
            return

        apply_round(game)
        store.save()

        final_msg = "✅ Раунд завершено!\n\n" + score_text(game, show_stats=True)
        await message.answer(final_msg, parse_mode="HTML")
        await notify_all_players(message.bot, game, final_msg, exclude_uid=uid)

        over = game_over_text(game)
        if over:
            await notify_all_players(message.bot, game, over)
            game["status"] = "finished"
            store.save()
        return

    # Each mode: only current awaiting can input
    awaiting_from = ar.get("awaiting_from")
    if awaiting_from != uid:
        if awaiting_from:
            awaiting_name = game["players"].get(awaiting_from, {}).get("name", "Гравець")
            await message.answer(f"⏳ Зараз черга <b>{awaiting_name}</b>. Почекай своєї черги.", parse_mode="HTML")
        return

    ar["delta"][uid] = pts
    ar["awaiting_from"] = None
    store.save()

    name_for = game["players"].get(uid, {}).get("name", uid)

    order = ar["order"]
    current_idx = order.index(uid) if uid in order else -1
    next_idx = current_idx + 1

    await notify_all_players(
        message.bot, game,
        f"✅ <b>{name_for}</b>: +{pts}\n\n🎴 <b>Раунд</b>\n\n{score_text(game, show_stats=True)}"
    )

    if next_idx < len(order):
        next_uid = order[next_idx]
        ar["awaiting_from"] = next_uid
        store.save()

        next_name = game["players"].get(next_uid, {}).get("name", "Гравець")
        await message.bot.send_message(
            chat_id=int(next_uid),
            text=f"<b>{next_name}</b>, введи свої бали числом (0 якщо виграв):",
            parse_mode="HTML"
        )
        return

    if all_filled(game):
        apply_round(game)
        store.save()

        final_msg = f"✅ <b>{name_for}</b>: +{pts}\n\n✅ Раунд завершено!\n\n{score_text(game, show_stats=True)}"
        await notify_all_players(message.bot, game, final_msg)

        over = game_over_text(game)
        if over:
            await notify_all_players(message.bot, game, over)
            game["status"] = "finished"
            store.save()

# ---------------- HEALTH SERVER ----------------

async def start_health_server():
    app = web.Application()

    async def health(_request):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    log.info("Health server started on port %s", port)

# ---------------- RUN ----------------

async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await start_health_server()
    log.info("Start polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
