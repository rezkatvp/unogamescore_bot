# UnoGameScore_bot.py
# Stable UNO scorekeeper bot for aiogram 3.25.0
# Logic: /new in group -> lobby join -> set target -> set mode -> rounds -> score/undo/cancel
# Storage: JSON file per chat

import asyncio
import json
import logging
import random
import string
from pathlib import Path
from typing import Dict, Any, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)

# ---------------- CONFIG ----------------

TOKEN = "8500117350:AAF7IadIGX7CAvymPc63MMwjj9Mf1ZWMr0A"  # <-- встав свій токен
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
        self.games_by_code: Dict[str, Dict[str, Any]] = {}  # код -> гра
        self.user_games: Dict[str, str] = {}  # user_id -> код гри
        self.load()

    def load(self):
        if not self.path.exists():
            self.games_by_code = {}
            self.user_games = {}
            log.info("Loaded games: 0")
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            # Старий формат (chat_id -> game) або новий (games_by_code, user_games)
            if "games_by_code" in raw:
                self.games_by_code = raw["games_by_code"]
                self.user_games = raw.get("user_games", {})
            else:
                # Міграція зі старого формату
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
                json.dumps({
                    "games_by_code": self.games_by_code,
                    "user_games": self.user_games
                }, ensure_ascii=False, indent=2),
                encoding="utf-8"
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
        }
        self.user_games[creator_uid] = code
        self.save()
        return code
    
    def get_user_game(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Отримує гру користувача"""
        code = self.user_games.get(user_id)
        if code:
            return self.games_by_code.get(code)
        return None
    
    def get_user_game_code(self, user_id: str) -> Optional[str]:
        """Отримує код гри користувача"""
        return self.user_games.get(user_id)
    
    def join_game(self, user_id: str, code: str) -> bool:
        """Приєднує користувача до гри"""
        code = code.upper()
        if code not in self.games_by_code:
            return False
        game = self.games_by_code[code]
        if game["status"] != "awaiting_players":
            return False
        if user_id in game["players"]:
            return True  # Вже в грі
        self.user_games[user_id] = code
        self.save()
        return True
    
    def leave_game(self, user_id: str):
        """Видаляє користувача з гри"""
        code = self.user_games.pop(user_id, None)
        if code and code in self.games_by_code:
            game = self.games_by_code[code]
            if user_id in game["players"]:
                del game["players"][user_id]
            if user_id in game["scores"]:
                del game["scores"][user_id]
            if user_id in game.get("wins", {}):
                del game["wins"][user_id]
            if user_id in game.get("total_points", {}):
                del game["total_points"][user_id]
            self.save()

store = Store(DATA_FILE)

# ---------------- UI ----------------

def kb_start_group() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Нова гра 🎮")]],
        resize_keyboard=True
    )

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎴 Новий раунд"), KeyboardButton(text="📊 Рахунок")],
            [KeyboardButton(text="↩️ Undo"), KeyboardButton(text="👤 Видалити гравця")],
            [KeyboardButton(text="🗑️ Cancel"), KeyboardButton(text="❓ Help")]
        ],
        resize_keyboard=True
    )

def ikb_lobby() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Приєднатись", callback_data="lobby:join")],
        [InlineKeyboardButton(text="▶️ Далі", callback_data="lobby:next")],
        [InlineKeyboardButton(text="🗑️ Скасувати", callback_data="lobby:cancel")],
    ])

def ikb_target() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="200", callback_data="target:200"),
            InlineKeyboardButton(text="500", callback_data="target:500"),
            InlineKeyboardButton(text="1000", callback_data="target:1000"),
        ],
        [InlineKeyboardButton(text="Інше", callback_data="target:other")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="target:back")],
    ])

def ikb_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ведучий вводить за всіх", callback_data="mode:leader")],
        [InlineKeyboardButton(text="👥 Кожен вводить сам", callback_data="mode:each")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:back")],
    ])

# Кнопки більше не потрібні - бот сам звертається до кожного гравця

def ikb_remove_player(game: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    for uid, p in game["players"].items():
        rows.append([InlineKeyboardButton(text=f"❌ {p['name']}", callback_data=f"remove:{uid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="remove:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def user_name(u) -> str:
    full = (getattr(u, "full_name", "") or "").strip()
    if full:
        return full
    username = getattr(u, "username", None)
    if username:
        return f"@{username}"
    return f"User{u.id}"

async def notify_all_players(bot: Bot, game: Dict[str, Any], message: str, exclude_uid: Optional[str] = None):
    """Відправляє повідомлення всім гравцям гри в приватні чати"""
    for uid in game["players"].keys():
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
        remaining = max(0, target - s)
        tag = ""
        if s >= target:
            tag = " 💀"
        elif s >= int(target * 0.75):
            tag = " 🔥"
        elif uid == leader_uid:
            tag = " 👑"
        
        # Показуємо прогрес до ліміту
        progress = f"{s}/{target}"
        if show_stats:
            win_count = wins.get(uid, 0)
            avg = round(total_points.get(uid, 0) / rounds_count, 1) if rounds_count > 0 else 0
            lines.append(f"<b>{p['name']}</b>: {progress}{tag} | 🏆{win_count} | 📊{avg}")
        else:
            lines.append(f"<b>{p['name']}</b>: {progress}{tag}")

    return "\n".join(lines)

def game_over_text(game: Dict[str, Any]) -> Optional[str]:
    target = int(game["target"])
    scores = {uid: int(v) for uid, v in game["scores"].items()}
    if not scores:
        return None
    if not any(v >= target for v in scores.values()):
        return None

    ranking = sorted(scores.items(), key=lambda x: x[1])
    winner_uid = ranking[0][0]
    winner_name = game["players"].get(winner_uid, {}).get("name", winner_uid)

    out = ["🏁 <b>ГРА ЗАКІНЧИЛАСЯ!</b>\nХтось набрав або перевищив ліміт.\n",
           "<b>Фінальний рейтинг (менше = краще):</b>"]
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
        "pending_inputs": {},  # used only in 'each' - зберігаємо хто має ввести
        "awaiting_from": None  # для режиму "each" - хто зараз має вводити
    }
    game["pinned_score_message_id"] = None  # для режиму "each"

def apply_round(game: Dict[str, Any]):
    ar = game["active_round"]
    delta = ar["delta"]
    for uid, pts in delta.items():
        pts_int = int(pts)
        game["scores"][uid] = int(game["scores"].get(uid, 0)) + pts_int
        game.setdefault("total_points", {})[uid] = game["total_points"].get(uid, 0) + pts_int
        # Якщо гравець виграв раунд (0 балів), збільшуємо лічильник перемог
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
        # Відкочуємо загальну суму балів
        if uid in game.get("total_points", {}):
            game["total_points"][uid] = max(0, game["total_points"][uid] - pts_int)
        # Відкочуємо перемогу якщо була
        if pts_int == 0 and uid in game.get("wins", {}):
            game["wins"][uid] = max(0, game["wins"].get(uid, 0) - 1)
    return True

def all_filled(game: Dict[str, Any]) -> bool:
    ar = game.get("active_round")
    if not ar:
        return False
    return all(v is not None for v in ar["delta"].values())

# ---------------- START / HELP ----------------

@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привіт! Я UNO-бот для підрахунку штрафних балів 🎴\n\n"
        "• /new — створити гру\n"
        "• /join код — приєднатись до гри\n"
        "• /score — рахунок\n"
        "• /history — історія раундів\n"
        "• /top — топ гравців\n"
        "• /undo — відкотити раунд\n"
        "• /leave — вийти з гри\n\n"
        "Всі повідомлення будуть в приватному чаті з ботом!"
    )

@router.message(F.text == "❓ Help")
@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "❓ <b>Підрахунок штрафів UNO</b>\n"
        "• Після раунду кожен гравець додає собі штрафні бали.\n"
        "• Гра закінчується, коли хтось набрав ≥ ліміт.\n"
        "• Перемагає той, у кого найменше штрафів.\n\n"
        "Команди: /new /score /undo /cancel",
        reply_markup=kb_main()
    )

# ---------------- NEW / CANCEL ----------------

@router.message(Command("new"))
async def new_game(message: Message):
    creator_uid = str(message.from_user.id)
    
    # Перевіряємо чи вже є активна гра
    existing_game = store.get_user_game(creator_uid)
    if existing_game and existing_game.get("status") != "finished":
        await message.answer("У тебе вже є активна гра. /leave щоб вийти.")
        return
    
    code = store.create_game(creator_uid)
    game = store.games_by_code[code]
    
    # Додаємо організатора як гравця
    nm = user_name(message.from_user)
    game["players"][creator_uid] = {"name": nm}
    game["scores"][creator_uid] = 0
    game["wins"][creator_uid] = 0
    game["total_points"][creator_uid] = 0
    store.save()
    
    await message.answer(
        f"🎮 <b>Нова гра UNO створена!</b>\n\n"
        f"Код гри: <code>{code}</code>\n\n"
        f"Поділись цим кодом з іншими гравцями.\n"
        f"Вони мають написати: <code>/join {code}</code>\n\n"
        f"Після того як всі приєднаються, напиши /start_game",
        reply_markup=ReplyKeyboardRemove()
    )

@router.message(Command("join"))
async def join_game(message: Message):
    uid = str(message.from_user.id)
    text = message.text or ""
    parts = text.split()
    
    if len(parts) < 2:
        await message.answer("Використання: /join код\nНаприклад: /join A1B2")
        return
    
    code = parts[1].upper()
    
    # Перевіряємо чи вже в іншій грі
    existing_code = store.user_games.get(uid)
    if existing_code and existing_code != code:
        await message.answer(f"Ти вже в іншій грі ({existing_code}). /leave щоб вийти.")
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
    
    # Повідомляємо організатора
    creator_uid = game["created_by"]
    try:
        await message.bot.send_message(
            chat_id=int(creator_uid),
            text=f"✅ <b>{nm}</b> приєднався до гри!\n\nГравці:\n{players_text(game)}"
        )
    except Exception as e:
        log.warning("Не вдалося повідомити організатора: %s", e)
    
    await message.answer(
        f"✅ Ти приєднався до гри <code>{code}</code>!\n\n"
        f"Гравці:\n{players_text(game)}\n\n"
        f"Очікуй поки організатор запустить гру."
    )

@router.message(Command("leave"))
async def leave_game(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)
    
    if not game:
        await message.answer("Ти не в грі.")
        return
    
    code = store.user_games.get(uid)
    store.leave_game(uid)
    
    await message.answer("✅ Ти вийшов з гри.")
    
    # Повідомляємо інших гравців
    if code:
        game = store.games_by_code.get(code)
        if game:
            player_name = user_name(message.from_user)
            for player_uid in game["players"].keys():
                if player_uid != uid:
                    try:
                        await message.bot.send_message(
                            chat_id=int(player_uid),
                            text=f"ℹ️ <b>{player_name}</b> вийшов з гри."
                        )
                    except Exception:
                        pass

@router.message(Command("start_game"))
async def start_game(message: Message):
    try:
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
        
        log.info(f"Starting game setup for user {uid}, status: {game['status']}")
        
        await message.answer(
            "🎯 <b>Оберіть ліміт балів</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="200", callback_data="target:200"),
                    InlineKeyboardButton(text="500", callback_data="target:500"),
                    InlineKeyboardButton(text="1000", callback_data="target:1000"),
                ],
                [InlineKeyboardButton(text="Інше", callback_data="target:other")],
            ])
        )
        log.info(f"Sent target selection message to user {uid}")
    except Exception as e:
        log.exception(f"Error in start_game: {e}")
        await message.answer(f"Помилка: {e}")

# ---------------- SCORE / UNDO ----------------

@router.message(Command("score"))
async def score(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)
    if not game:
        await message.answer("Ти не в грі. /new щоб створити або /join код щоб приєднатись.")
        return
    if game.get("status") not in {"running", "finished"}:
        await message.answer("Гра ще не запущена.")
        return
    await message.answer(score_text(game, show_stats=True))

@router.message(Command("undo"))
async def undo(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)
    if not game:
        await message.answer("Ти не в грі.")
        return
    if game.get("active_round"):
        await message.answer("Зараз активний раунд. Завершіть його.")
        return
    if not undo_round(game):
        await message.answer("Немає що відкотити.")
        return
    store.save()
    await message.answer("↩️ Останній раунд скасовано.\n\n" + score_text(game, show_stats=True))
    await notify_all_players(message.bot, game, f"↩️ Останній раунд скасовано.\n\n{score_text(game, show_stats=True)}", exclude_uid=uid)

@router.message(Command("history"))
async def history_cmd(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)
    if not game or game.get("status") not in {"running", "finished"}:
        await message.answer("Гра ще не запущена.")
        return
    
    rounds = game.get("rounds", [])
    if not rounds:
        await message.answer("Поки що немає завершених раундів.")
        return
    
    # Показуємо останні 10 раундів
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
    
    await message.answer("\n\n".join(lines))

@router.message(Command("top"))
async def top_cmd(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)
    if not game or game.get("status") not in {"running", "finished"}:
        await message.answer("Гра ще не запущена.")
        return
    
    players = game["players"]
    scores = game["scores"]
    wins = game.get("wins", {})
    rounds_count = len(game.get("rounds", []))
    total_points = game.get("total_points", {})
    
    if not players:
        await message.answer("Немає гравців.")
        return
    
    # Топ за перемогами
    top_wins = sorted(
        [(uid, wins.get(uid, 0), players[uid]["name"]) for uid in players.keys()],
        key=lambda x: x[1],
        reverse=True
    )
    
    # Топ за найменшими балами
    top_scores = sorted(
        [(uid, int(scores.get(uid, 0)), players[uid]["name"]) for uid in players.keys()],
        key=lambda x: x[1]
    )
    
    lines = ["🏆 <b>Топ гравців</b>\n"]
    lines.append("<b>За перемогами:</b>")
    for i, (uid, win_count, name) in enumerate(top_wins[:3], start=1):
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        lines.append(f"{medal} {name}: {win_count} перемог")
    
    lines.append("\n<b>За найменшими балами:</b>")
    for i, (uid, score, name) in enumerate(top_scores[:3], start=1):
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        avg = round(total_points.get(uid, 0) / rounds_count, 1) if rounds_count > 0 else 0
        lines.append(f"{medal} {name}: {score} балів (середнє: {avg})")
    
    await message.answer("\n".join(lines))

# Застарілі функції для груп видалені - тепер все працює через приватні чати

# ---------------- TARGET CALLBACKS ----------------

@router.callback_query(F.data.startswith("target:"))
async def target_pick(cb: CallbackQuery):
    try:
        uid = str(cb.from_user.id)
        game = store.get_user_game(uid)
        
        log.info(f"target_pick callback from user {uid}, game status: {game.get('status') if game else 'None'}")
        
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

        if action == "back":
            game["status"] = "awaiting_players"
            store.save()
            await cb.answer()
            await cb.message.edit_text("Повернулись до очікування гравців.")
            return

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

        log.info(f"Target set to {game['target']}, moving to setup_mode")
        
        await cb.answer()
        await cb.message.edit_text("⚙️ <b>Оберіть режим введення балів</b>:", reply_markup=ikb_mode())
    except Exception as e:
        log.exception(f"Error in target_pick: {e}")
        await cb.answer(f"Помилка: {e}", show_alert=True)

# ---------------- MODE CALLBACKS ----------------

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

    if action == "back":
        game["status"] = "setup_target"
        store.save()
        await cb.answer()
        await cb.message.edit_text(
            "🎯 <b>Оберіть ліміт балів</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="200", callback_data="target:200"),
                    InlineKeyboardButton(text="500", callback_data="target:500"),
                    InlineKeyboardButton(text="1000", callback_data="target:1000"),
                ],
                [InlineKeyboardButton(text="Інше", callback_data="target:other")],
            ])
        )
        return

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
        f"Напиши /round щоб почати раунд"
    )
    
    # Повідомляємо всіх гравців
    await notify_all_players(
        cb.bot, game,
        f"✅ <b>Гра запущена!</b>\n\n"
        f"Код: <code>{code}</code>\n\n"
        f"Ліміт: <b>{game['target']}</b>\n"
        f"Режим: <b>{mode_text}</b>\n\n"
        f"Напиши /round щоб почати раунд",
        exclude_uid=uid
    )

# ---------------- ROUND START ----------------

@router.message(Command("round"))
async def round_start(message: Message):
    uid = str(message.from_user.id)
    game = store.get_user_game(uid)

    if not game:
        await message.answer("Ти не в грі. /new щоб створити або /join код щоб приєднатись.")
        return
    
    if game.get("status") == "finished":
        await message.answer("Гра вже завершена. Створіть нову гру /new")
        return
    
    if game.get("status") != "running":
        await message.answer("Гра не запущена.")
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
        await message.answer(
            f"✍️ Введи бали для <b>{first_name}</b>:",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        # Режим "кожен сам": відправляємо окремі повідомлення кожному гравцю
        score_text_msg = "🎴 <b>Раунд старт!</b>\n\n" + score_text(game, show_stats=True)
        
        # Повідомляємо всіх гравців про старт раунду
        await notify_all_players(message.bot, game, score_text_msg)
        
        # Встановлюємо першого гравця як того хто має вводити
        if ar["order"]:
            first_uid = ar["order"][0]
            ar["awaiting_from"] = first_uid
            store.save()
            
            first_name = game["players"][first_uid]["name"]
            await message.bot.send_message(
                chat_id=int(first_uid),
                text=f"<b>{first_name}</b>, введи свої бали числом (0 якщо виграв):",
                parse_mode="HTML"
            )

# ---------------- EACH MODE CALLBACKS ----------------

# Кнопки вибору себе більше не потрібні - бот сам знає хто має вводити

# Кнопка "Завершити раунд" більше не потрібна - бот сам завершує коли всі ввели

# ---------------- NUMERIC INPUT (SAFE!) ----------------
# Важливо: цей хендлер НЕ повинен ловити все підряд.
# Він працює тільки якщо:
# 1) ми чекаємо кастомний ліміт від організатора, або
# 2) є active_round і очікуємо очки.

@router.message()
async def numeric_router(message: Message):
    try:
        text = (message.text or "").strip()
        # Пропускаємо команди та порожні повідомлення
        if not text or text.startswith("/"):
            return
        
        # Пропускаємо якщо це не число (для швидшої обробки)
        if not text.isdigit() and text not in ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]:
            # Спробуємо перевірити чи це число
            try:
                int(text)
            except ValueError:
                return  # Не число, пропускаємо

        uid = str(message.from_user.id)
        
        # Отримуємо гру користувача
        game = store.get_user_game(uid)
        if not game:
            return

        # 1) custom target input
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
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✍️ Ведучий вводить за всіх", callback_data="mode:leader")],
                        [InlineKeyboardButton(text="👥 Кожен вводить сам", callback_data="mode:each")],
                        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:back")],
                    ])
                )
            except ValueError:
                await message.answer("Введи ціле число. Наприклад: 500")
            return

        # 2) points input only if active round exists
        ar = game.get("active_round")
        if not ar:
            return

        # parse points
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
                await message.answer(f"✍️ Введи бали для <b>{nxt_name}</b>:")
                return

            apply_round(game)
            store.save()
            
            final_msg = "✅ Раунд завершено!\n\n" + score_text(game, show_stats=True)
            await message.answer(final_msg)
            await notify_all_players(message.bot, game, final_msg, exclude_uid=uid)

            over = game_over_text(game)
            if over:
                await notify_all_players(message.bot, game, over)
                # Автоматично завершуємо гру
                game["status"] = "finished"
                store.save()
            return

        # Each mode: перевіряємо чи це той хто має вводити
        awaiting_from = ar.get("awaiting_from")
        if awaiting_from != uid:
            # Не твоя черга - повідомляємо хто має вводити
            if awaiting_from:
                awaiting_name = game["players"].get(awaiting_from, {}).get("name", "Гравець")
                await message.answer(f"⏳ Зараз черга <b>{awaiting_name}</b>. Почекай своєї черги.", parse_mode="HTML")
            return

        # Знаходимо для кого вводимо бали (завжди для себе)
        ar["delta"][uid] = pts
        ar["awaiting_from"] = None
        store.save()

        name_for = game["players"].get(uid, {}).get("name", uid)
        
        # Знаходимо наступного гравця
        order = ar["order"]
        current_idx = order.index(uid) if uid in order else -1
        next_idx = current_idx + 1
        
        # Повідомляємо всіх про оновлення
        await notify_all_players(
            message.bot, game,
            f"✅ <b>{name_for}</b>: +{pts}\n\n🎴 <b>Раунд</b>\n\n{score_text(game, show_stats=True)}"
        )

        # Якщо є ще гравці які не ввели
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

        # Всі ввели - завершуємо раунд
        if all_filled(game):
            apply_round(game)
            store.save()
            
            final_msg = f"✅ <b>{name_for}</b>: +{pts}\n\n✅ Раунд завершено!\n\n{score_text(game, show_stats=True)}"
            await notify_all_players(message.bot, game, final_msg)

            over = game_over_text(game)
            if over:
                await notify_all_players(message.bot, game, over)
                # Автоматично завершуємо гру
                game["status"] = "finished"
                store.save()
    except Exception as e:
        log.exception("numeric_router error: %s", e)
        try:
            await message.answer("Сталася технічна помилка при обробці балів. Спробуй ще раз або /score.")
        except Exception:
            pass

# ---------------- RUN ----------------

async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Start polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())