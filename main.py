import os
import asyncio
import secrets
import string
import hashlib
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, FSInputFile
from aiocryptopay import AioCryptoPay, Networks
import httpx

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import Column, Integer, BigInteger, String, Boolean, Float, DateTime, Text, func, select, update, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

# ======================== CONFIG ========================

BOT_TOKEN        = os.getenv("BOT_TOKEN", "8897980437:AAH6LAvjq4H_t8amJKJ7pholkg6WTHgKgxU")
CRYPTO_PAY_API_KEY = os.getenv("CRYPTO_PAY_API_KEY", "631099:AAc8WyW8iQpFepMncLt77HDLg1eGJXcWWNJ")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "7541535601"))
DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql://ambella:EwpDbvPjd6DxO7ZDoEiVCpFeJkBFnJ73@dpg-daemrson74is73eij1c0-a.ohio-postgres.render.com/ambella")
BACKEND_URL      = os.getenv("BACKEND_URL", "https://ambella-backend.onrender.com")
ADMIN_KEY        = os.getenv("ADMIN_KEY", "21e8fbb91e54818f1d05c59a9bda22d8")

PRODUCT_NAME = "Ambella"
LOADER_FILE_PATH = "files/loader.exe"
SUBSCRIPTION_PLANS = {
    "1d":      {"title": "1 день",    "days": 1,    "price": 0.49, "emoji": "⚡", "badge": "Тест-драйв"},
    "7d":      {"title": "7 дней",   "days": 7,    "price": 1.99, "emoji": "🔥", "badge": "Популярно"},
    "1m":      {"title": "1 месяц",  "days": 30,   "price": 3.99, "emoji": "💎", "badge": "Выгодно"},
    "forever": {"title": "Навсегда", "days": None, "price": 8.99, "emoji": "👑", "badge": "Максимум"},
}

PRODUCT_DESCRIPTION = f"""<b>⚔️ {PRODUCT_NAME}</b>
<i>Приватный чит-клиент для Minecraft RustMe</i>

╔════════════════════
║ ✨ <b>Возможности</b>
╠ AimBot с предиктом
╠ ESP / SoundESP / WallHack
╠ Chams / X-Ray / Night Mode
╠ Zoom / ViewModel Edit
╠ Auto Sprint / Time Scale
╚ Crosshair Overlay

🛡 <b>Защита:</b> привязка к 1 ПК по HWID
💳 <b>Оплата:</b> CryptoBot, USDT
"""

# ======================== UTILS ========================

def generate_login(length=8):
    chars = string.ascii_lowercase + string.digits
    return "oyuz_" + ''.join(secrets.choice(chars) for _ in range(length))

import bcrypt

def generate_password(length=12):
    chars = string.ascii_letters + string.digits + "!@#$"
    return ''.join(secrets.choice(chars) for _ in range(length))

def hash_password(password: str) -> str:
    # Truncate password to 72 bytes due to bcrypt specification limits
    pw_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pw_bytes, salt).decode('utf-8')

def generate_subscription_key(length=20):
    chars = string.ascii_lowercase + string.digits
    return "ar_" + ''.join(secrets.choice(chars) for _ in range(length))

def calculate_subscription_expires(current_expires, days):
    if days is None:
        return None  # NULL = forever in PostgreSQL
    now = datetime.utcnow()
    base = now
    if current_expires and isinstance(current_expires, datetime) and current_expires > now:
        base = current_expires
    return base + timedelta(days=days)

# ======================== DATABASE (PostgreSQL) ========================

class Base(DeclarativeBase):
    pass

class Account(Base):
    __tablename__ = "accounts"
    id                   = Column(Integer, primary_key=True, autoincrement=True)
    user_id              = Column(String(64), unique=True, nullable=True)
    login                = Column(String(64), unique=True, nullable=False)
    password_hash        = Column(String(128), nullable=False)
    hwid                 = Column(String(256), default="")
    is_paid              = Column(Boolean, default=False)
    payment_date         = Column(DateTime, nullable=True)
    subscription_expires = Column(DateTime, nullable=True)  # NULL = forever
    registered_at        = Column(DateTime, default=datetime.utcnow)

class Payment(Base):
    __tablename__ = "payments"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(BigInteger, nullable=False)
    invoice_id = Column(BigInteger, nullable=False)
    amount     = Column(Float, nullable=False)
    currency   = Column(String(16))
    status     = Column(String(32))
    plan_id    = Column(String(16), default="1m")
    created_at = Column(DateTime, default=datetime.utcnow)

class SubscriptionKey(Base):
    __tablename__ = "subscription_keys"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    key_value  = Column(String(64), unique=True, nullable=False)
    plan_id    = Column(String(16), nullable=False)
    is_used    = Column(Boolean, default=False)
    used_by    = Column(BigInteger, nullable=True)
    used_at    = Column(DateTime, nullable=True)
    created_by = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Database:
    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def init(self):
        url = DATABASE_URL
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        
        # Check if using Render internal hostname from outside Render
        if "-a/" in url and ".render.com" not in url and "RENDER" not in os.environ:
            print("\n" + "="*70)
            print("❌ ВНИМАНИЕ: Вы используете Internal Database URL с домашнего ПК!")
            print("Адрес '@dpg-...-a' доступен ТОЛЬКО внутри серверов Render.")
            print("Для запуска на домашнем ПК скопируйте 'External Database URL' из Render")
            print("(он заканчивается на .render.com, например: .frankfurt-postgres.render.com)")
            print("="*70 + "\n")

        self.engine = create_async_engine(url, echo=False, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except Exception as e:
            print(f"\n❌ Ошибка подключения к базе данных: {e}\n")
            raise

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def get_account_by_user_id(self, user_id: int) -> dict | None:
        async with self.session() as s:
            row = (await s.execute(select(Account).where(Account.user_id == str(user_id)))).scalar_one_or_none()
            return self._acc_to_dict(row)

    async def get_account_by_login(self, login: str) -> dict | None:
        async with self.session() as s:
            row = (await s.execute(select(Account).where(Account.login == login))).scalar_one_or_none()
            return self._acc_to_dict(row)

    def _acc_to_dict(self, row: Account | None) -> dict | None:
        if not row:
            return None
        return {
            "id": row.id, "user_id": row.user_id, "login": row.login,
            "password_hash": row.password_hash, "hwid": row.hwid or "",
            "is_paid": row.is_paid, "payment_date": row.payment_date,
            "subscription_expires": row.subscription_expires,
            "registered_at": row.registered_at,
        }

    def _is_active(self, acc: dict) -> bool:
        if not acc or not acc["is_paid"]:
            return False
        exp = acc["subscription_expires"]
        if exp is None:
            return True  # forever
        return exp > datetime.utcnow()

    async def create_account(self, user_id: int, login: str, password_hash: str) -> int:
        async with self.session() as s:
            acc = Account(user_id=str(user_id), login=login, password_hash=password_hash,
                          registered_at=datetime.utcnow())
            s.add(acc)
            await s.commit()
            await s.refresh(acc)
            return acc.id

    async def set_paid(self, account_id: int, expires: datetime | None):
        async with self.session() as s:
            await s.execute(
                update(Account).where(Account.id == account_id)
                .values(is_paid=True, payment_date=datetime.utcnow(), subscription_expires=expires))
            await s.commit()

    async def set_hwid(self, account_id: int, hwid: str):
        async with self.session() as s:
            await s.execute(update(Account).where(Account.id == account_id).values(hwid=hwid))
            await s.commit()

    async def change_password(self, account_id: int, new_hash: str):
        async with self.session() as s:
            await s.execute(update(Account).where(Account.id == account_id).values(password_hash=new_hash))
            await s.commit()

    async def add_payment(self, user_id, invoice_id, amount, currency, status, plan_id="1m"):
        async with self.session() as s:
            p = Payment(user_id=user_id, invoice_id=invoice_id, amount=amount,
                        currency=currency, status=status, plan_id=plan_id,
                        created_at=datetime.utcnow())
            s.add(p)
            await s.commit()

    async def get_pending_invoice(self, user_id: int):
        async with self.session() as s:
            row = (await s.execute(
                select(Payment.invoice_id, Payment.plan_id)
                .where(Payment.user_id == user_id, Payment.status == "pending")
                .order_by(Payment.id.desc()).limit(1)
            )).first()
            return row

    async def update_payment_status(self, invoice_id: int, status: str):
        async with self.session() as s:
            await s.execute(update(Payment).where(Payment.invoice_id == invoice_id).values(status=status))
            await s.commit()

    async def get_all_accounts(self) -> list:
        async with self.session() as s:
            rows = (await s.execute(select(Account))).scalars().all()
            return [self._acc_to_dict(r) for r in rows]

    async def get_stats(self) -> dict:
        async with self.session() as s:
            total  = (await s.execute(select(func.count()).select_from(Account))).scalar()
            paid   = (await s.execute(select(func.count()).select_from(Account).where(Account.is_paid == True))).scalar()
            revenue = (await s.execute(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid")
            )).scalar()
            return {"total": total, "paid": paid, "revenue": float(revenue)}

    async def delete_account(self, user_id: int) -> bool:
        async with self.session() as s:
            acc = (await s.execute(select(Account).where(Account.user_id == str(user_id)))).scalar_one_or_none()
            if not acc:
                return False
            await s.execute(delete(Payment).where(Payment.user_id == user_id))
            await s.delete(acc)
            await s.commit()
            return True

    async def create_subscription_key(self, plan_id: str, created_by: int) -> str:
        for _ in range(10):
            key_value = generate_subscription_key()
            try:
                async with self.session() as s:
                    k = SubscriptionKey(key_value=key_value, plan_id=plan_id,
                                        created_by=created_by, created_at=datetime.utcnow())
                    s.add(k)
                    await s.commit()
                    return key_value
            except Exception:
                continue
        raise RuntimeError("Failed to generate unique key")

    async def get_subscription_key(self, key_value: str) -> dict | None:
        async with self.session() as s:
            row = (await s.execute(
                select(SubscriptionKey).where(SubscriptionKey.key_value == key_value)
            )).scalar_one_or_none()
            if not row:
                return None
            return {"id": row.id, "key_value": row.key_value, "plan_id": row.plan_id,
                    "is_used": row.is_used, "used_by": row.used_by}

    async def use_subscription_key(self, key_id: int, user_id: int):
        async with self.session() as s:
            await s.execute(
                update(SubscriptionKey).where(SubscriptionKey.id == key_id)
                .values(is_used=True, used_by=user_id, used_at=datetime.utcnow()))
            await s.commit()

    async def extend_subscription_via_backend(self, account_id: int, login: str, days: int | None):
        """Also notify backend API to update its own DB."""
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(
                    f"{BACKEND_URL}/api/admin/accounts/{login}/extend",
                    json={"days": days},
                    headers={"X-Admin-Key": ADMIN_KEY}
                )
            except Exception:
                pass
        expires = calculate_subscription_expires(None, days)
        await self.set_paid(account_id, expires)

    async def close(self):
        if self.engine:
            await self.engine.dispose()


# ======================== CRYPTO PAY ========================

class CryptoPayService:
    def __init__(self, api_token: str):
        self.client = AioCryptoPay(token=api_token, network=Networks.MAIN_NET)

    async def create_invoice(self, amount: float, description: str, payload: str) -> dict:
        invoice = await self.client.create_invoice(
            asset="USDT", amount=amount, description=description,
            payload=payload, expires_in=1800
        )
        return {
            "invoice_id": invoice.invoice_id,
            "bot_url": invoice.bot_invoice_url,
            "mini_app_url": getattr(invoice, 'mini_app_invoice_url', None) or invoice.bot_invoice_url,
        }

    async def get_invoice_status(self, invoice_id: int) -> dict:
        invoices = await self.client.get_invoices(invoice_ids=[invoice_id])
        if invoices:
            inv = invoices[0]
            return {
                "invoice_id": inv.invoice_id, "status": inv.status,
                "amount": str(inv.amount), "currency": inv.asset,
                "payload": getattr(inv, 'payload', ''),
            }
        return {}

    async def close(self):
        await self.client.close()


# ======================== FSM ========================

class RegisterState(StatesGroup):
    waiting_for_login    = State()
    waiting_for_password = State()

class KeyState(StatesGroup):
    waiting_for_key = State()

class AdminKeyState(StatesGroup):
    waiting_for_count = State()


# ======================== BOT ========================

class OyuzBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.dp  = Dispatcher(storage=MemoryStorage())
        self.crypto = CryptoPayService(CRYPTO_PAY_API_KEY)
        self.db     = Database()
        self._handlers()

    # ---------- Keyboard helpers ----------

    def _main_kb(self, registered=False):
        kb = []
        if not registered:
            kb.append([InlineKeyboardButton(text="🚀 Создать аккаунт", callback_data="register")])
            kb.append([InlineKeyboardButton(text="🔑 Уже есть аккаунт", callback_data="login")])
        else:
            kb.append([InlineKeyboardButton(text="💎 Купить / продлить доступ", callback_data="buy_product")])
            kb.append([InlineKeyboardButton(text="👤 Кабинет пользователя",    callback_data="my_profile")])
        return InlineKeyboardMarkup(inline_keyboard=kb)

    def _back_kb(self):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])

    def _plans_kb(self, prefix: str):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{p['emoji']} {p['title']} — {p['price']} USDT · {p['badge']}",
                callback_data=f"{prefix}{pid}")]
            for pid, p in SUBSCRIPTION_PLANS.items()
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])

    def _plans_text(self):
        return "\n".join(
            f"{p['emoji']} <b>{p['title']}</b> — <code>{p['price']} USDT</code> · {p['badge']}"
            for p in SUBSCRIPTION_PLANS.values()
        )

    # ---------- Subscription expiry text ----------

    def _exp_text(self, acc: dict) -> str:
        if not acc["is_paid"]:
            return "❌ Нет подписки"
        exp = acc["subscription_expires"]
        if exp is None:
            return "👑 Навсегда"
        now = datetime.utcnow()
        if exp <= now:
            return "⚠️ Истекла"
        delta = exp - now
        days = delta.days
        if days == 0:
            return f"⏳ Осталось менее дня"
        return f"✅ До {exp.strftime('%d.%m.%Y')} ({days} д.)"

    # ---------- Handlers ----------

    def _handlers(self):
        dp = self.dp
        dp.message.register(self._start,    CommandStart())
        dp.message.register(self._register, Command("register"))
        dp.message.register(self._login,    Command("login"))
        dp.message.register(self._profile,  Command("profile"))
        dp.message.register(self._help,     Command("help"))
        if ADMIN_ID:
            dp.message.register(self._admin,  Command("admin"))
            dp.message.register(self._delete, Command("delete"))
            dp.callback_query.register(self._cb_confirm_delete, F.data.startswith("confirm_delete_"))

        dp.callback_query.register(self._cb_register,     F.data == "register")
        dp.callback_query.register(self._cb_login,        F.data == "login")
        dp.callback_query.register(self._cb_buy,          F.data == "buy_product")
        dp.callback_query.register(self._cb_pay,          F.data.startswith("buy_confirm_"))
        dp.callback_query.register(self._cb_check,        F.data == "check_payment")
        dp.callback_query.register(self._cb_back,         F.data == "back_to_menu")
        dp.callback_query.register(self._cb_profile,      F.data == "my_profile")
        dp.callback_query.register(self._cb_changepass,   F.data == "change_password")
        dp.callback_query.register(self._cb_activate_key, F.data == "activate_key")
        if ADMIN_ID:
            dp.callback_query.register(self._cb_admin_users,   F.data == "admin_users")
            dp.callback_query.register(self._cb_admin_stats,   F.data == "admin_stats")
            dp.callback_query.register(self._cb_admin_keys,    F.data == "admin_keys")
            dp.callback_query.register(self._cb_admin_key_plan, F.data.startswith("admin_key_"))

        from aiogram.filters import StateFilter
        dp.message.register(self._reg_login,           StateFilter(RegisterState.waiting_for_login))
        dp.message.register(self._reg_password,        StateFilter(RegisterState.waiting_for_password))
        dp.message.register(self._activate_key_message, StateFilter(KeyState.waiting_for_key))
        dp.message.register(self._admin_key_count_message, StateFilter(AdminKeyState.waiting_for_count))

    # /start
    async def _start(self, m: Message):
        acc = await self.db.get_account_by_user_id(m.from_user.id)
        registered = acc is not None
        text = (
            f"👋 Привет, <b>{m.from_user.first_name}</b>!\n\n"
            f"{PRODUCT_DESCRIPTION}\n"
            f"{'✅ Ты уже зарегистрирован' if registered else '❌ Аккаунт не найден — создай его'}"
        )
        await m.answer(text, parse_mode="HTML", reply_markup=self._main_kb(registered))

    # /register
    async def _register(self, m: Message):
        acc = await self.db.get_account_by_user_id(m.from_user.id)
        if acc:
            await m.answer("✅ У тебя уже есть аккаунт.", reply_markup=self._main_kb(True))
            return
        await m.answer("👤 Введи желаемый логин (только буквы и цифры):")
        # handled in FSM below

    # callback register
    async def _cb_register(self, c: CallbackQuery, state: FSMContext):
        acc = await self.db.get_account_by_user_id(c.from_user.id)
        if acc:
            await c.message.edit_text("✅ Аккаунт уже существует.", reply_markup=self._main_kb(True))
            return
        await c.message.edit_text("👤 Придумай логин (только буквы и цифры, 4–20 символов):")
        await state.set_state(RegisterState.waiting_for_login)
        await c.answer()

    async def _reg_login(self, m: Message, state: FSMContext):
        login = m.text.strip()
        if not login.isalnum() or len(login) < 4 or len(login) > 20:
            await m.answer("❌ Логин: только буквы и цифры, 4–20 символов. Попробуй ещё:")
            return
        existing = await self.db.get_account_by_login(login)
        if existing:
            await m.answer("❌ Этот логин занят. Введи другой:")
            return
        await state.update_data(login=login)
        await m.answer("🔒 Теперь придумай пароль (минимум 6 символов):")
        await state.set_state(RegisterState.waiting_for_password)

    async def _reg_password(self, m: Message, state: FSMContext):
        password = m.text.strip()
        if len(password) < 6:
            await m.answer("❌ Пароль слишком короткий. Минимум 6 символов:")
            return
        data = await state.get_data()
        login = data["login"]
        pw_hash = hash_password(password)
        await self.db.create_account(m.from_user.id, login, pw_hash)
        await state.clear()
        await m.answer(
            f"✅ <b>Аккаунт создан!</b>\n\n"
            f"👤 Логин: <code>{login}</code>\n"
            f"🔒 Пароль: <code>{password}</code>\n\n"
            f"💾 Сохрани эти данные — они нужны для входа в лоадер!",
            parse_mode="HTML", reply_markup=self._main_kb(True)
        )

    # callback login (info)
    async def _cb_login(self, c: CallbackQuery):
        acc = await self.db.get_account_by_user_id(c.from_user.id)
        if not acc:
            await c.message.edit_text("❌ Аккаунт не найден. Сначала зарегистрируйся.",
                                      reply_markup=self._main_kb(False))
            return
        await c.message.edit_text(
            f"🔑 Твои данные для лоадера:\n\n"
            f"👤 Логин: <code>{acc['login']}</code>\n"
            f"🔒 Пароль: скрыт (сохранён при регистрации)\n\n"
            f"Если забыл пароль — смени его в профиле.",
            parse_mode="HTML", reply_markup=self._main_kb(True)
        )
        await c.answer()

    # /login command
    async def _login(self, m: Message):
        await self._cb_login_msg(m)

    async def _cb_login_msg(self, m: Message):
        acc = await self.db.get_account_by_user_id(m.from_user.id)
        if not acc:
            await m.answer("❌ Аккаунт не найден.", reply_markup=self._main_kb(False))
            return
        await m.answer(f"👤 Логин: <code>{acc['login']}</code>",
                       parse_mode="HTML", reply_markup=self._main_kb(True))

    # /profile command
    async def _profile(self, m: Message):
        await self._show_profile(m)

    async def _show_profile(self, m: Message):
        acc = await self.db.get_account_by_user_id(m.from_user.id)
        if not acc:
            await m.answer("❌ Аккаунт не найден.", reply_markup=self._main_kb(False))
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Активировать ключ",  callback_data="activate_key")],
            [InlineKeyboardButton(text="🔒 Сменить пароль",     callback_data="change_password")],
            [InlineKeyboardButton(text="⬅️ Назад",              callback_data="back_to_menu")],
        ])
        await m.answer(
            f"👤 <b>Профиль</b>\n\n"
            f"Логин: <code>{acc['login']}</code>\n"
            f"HWID:  <code>{acc['hwid'] or 'не привязан'}</code>\n"
            f"Подписка: {self._exp_text(acc)}\n"
            f"Зарегистрирован: {acc['registered_at'].strftime('%d.%m.%Y') if acc['registered_at'] else '—'}",
            parse_mode="HTML", reply_markup=kb
        )

    async def _cb_profile(self, c: CallbackQuery):
        acc = await self.db.get_account_by_user_id(c.from_user.id)
        if not acc:
            await c.message.edit_text("❌ Аккаунт не найден.", reply_markup=self._main_kb(False))
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Активировать ключ",  callback_data="activate_key")],
            [InlineKeyboardButton(text="🔒 Сменить пароль",     callback_data="change_password")],
            [InlineKeyboardButton(text="⬅️ Назад",              callback_data="back_to_menu")],
        ])
        await c.message.edit_text(
            f"👤 <b>Профиль</b>\n\n"
            f"Логин: <code>{acc['login']}</code>\n"
            f"HWID:  <code>{acc['hwid'] or 'не привязан'}</code>\n"
            f"Подписка: {self._exp_text(acc)}\n"
            f"Зарегистрирован: {acc['registered_at'].strftime('%d.%m.%Y') if acc['registered_at'] else '—'}",
            parse_mode="HTML", reply_markup=kb
        )
        await c.answer()

    # Change password
    async def _cb_changepass(self, c: CallbackQuery, state: FSMContext):
        await c.message.edit_text("🔒 Введи новый пароль (минимум 6 символов):")
        await state.set_state(RegisterState.waiting_for_password)
        await state.update_data(change_pass=True, login=None)
        await c.answer()

    # Buy
    async def _cb_buy(self, c: CallbackQuery):
        acc = await self.db.get_account_by_user_id(c.from_user.id)
        if not acc:
            await c.message.edit_text("❌ Сначала создай аккаунт.", reply_markup=self._main_kb(False))
            return
        await c.message.edit_text(
            f"💎 <b>Выбери тариф</b>\n\n{self._plans_text()}",
            parse_mode="HTML", reply_markup=self._plans_kb("buy_confirm_")
        )
        await c.answer()

    async def _cb_pay(self, c: CallbackQuery):
        plan_id = c.data.replace("buy_confirm_", "")
        plan = SUBSCRIPTION_PLANS.get(plan_id)
        if not plan:
            await c.answer("❌ Тариф не найден", show_alert=True)
            return
        acc = await self.db.get_account_by_user_id(c.from_user.id)
        if not acc:
            await c.answer("❌ Аккаунт не найден", show_alert=True)
            return

        description = f"{PRODUCT_NAME} — {plan['title']}"
        payload = f"{acc['id']}:{plan_id}"
        try:
            inv = await self.crypto.create_invoice(plan["price"], description, payload)
        except Exception as e:
            await c.message.edit_text(f"❌ Ошибка создания инвойса: {e}", reply_markup=self._back_kb())
            return

        await self.db.add_payment(c.from_user.id, inv["invoice_id"], plan["price"], "USDT", "pending", plan_id)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=inv["mini_app_url"])],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="check_payment")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
        ])
        await c.message.edit_text(
            f"{plan['emoji']} <b>{plan['title']}</b> — <code>{plan['price']} USDT</code>\n\n"
            f"Оплати и нажми «Проверить оплату».",
            parse_mode="HTML", reply_markup=kb
        )
        await c.answer()

    async def _cb_check(self, c: CallbackQuery):
        inv_row = await self.db.get_pending_invoice(c.from_user.id)
        if not inv_row:
            await c.answer("❌ Активный счёт не найден", show_alert=True)
            return

        invoice_id, plan_id = inv_row[0], inv_row[1]
        try:
            status_data = await self.crypto.get_invoice_status(invoice_id)
        except Exception as e:
            await c.answer(f"❌ Ошибка: {e}", show_alert=True)
            return

        status = status_data.get("status", "")
        if status != "paid":
            await c.answer("⏳ Ещё не оплачено", show_alert=True)
            return

        await self.db.update_payment_status(invoice_id, "paid")

        acc = await self.db.get_account_by_user_id(c.from_user.id)
        plan = SUBSCRIPTION_PLANS.get(plan_id, SUBSCRIPTION_PLANS["1m"])
        days = plan.get("days")
        expires = calculate_subscription_expires(acc.get("subscription_expires"), days)
        await self.db.set_paid(acc["id"], expires)

        # Notify backend
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(
                    f"{BACKEND_URL}/api/admin/accounts/{acc['login']}/extend",
                    json={"days": days},
                    headers={"X-Admin-Key": ADMIN_KEY}
                )
            except Exception:
                pass

        exp_str = "Навсегда" if expires is None else expires.strftime("%d.%m.%Y")
        await c.message.edit_text(
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
            f"📅 Подписка до: <b>{exp_str}</b>",
            parse_mode="HTML", reply_markup=self._main_kb(True)
        )
        await c.answer("✅ Подписка активирована!")

    # Activate key
    async def _cb_activate_key(self, c: CallbackQuery, state: FSMContext):
        await c.message.edit_text("🔑 Введи ключ подписки:")
        await state.set_state(KeyState.waiting_for_key)
        await c.answer()

    async def _activate_key_message(self, m: Message, state: FSMContext):
        key_value = m.text.strip()
        key = await self.db.get_subscription_key(key_value)
        if not key:
            await m.answer("❌ Ключ не найден.")
            await state.clear()
            return
        if key["is_used"]:
            await m.answer("❌ Ключ уже использован.")
            await state.clear()
            return

        acc = await self.db.get_account_by_user_id(m.from_user.id)
        if not acc:
            await m.answer("❌ Сначала создай аккаунт.")
            await state.clear()
            return

        plan_id = key["plan_id"]
        plan = SUBSCRIPTION_PLANS.get(plan_id, SUBSCRIPTION_PLANS["1m"])
        days = plan.get("days")
        expires = calculate_subscription_expires(acc.get("subscription_expires"), days)
        await self.db.set_paid(acc["id"], expires)
        await self.db.use_subscription_key(key["id"], m.from_user.id)

        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.post(
                    f"{BACKEND_URL}/api/admin/accounts/{acc['login']}/extend",
                    json={"days": days},
                    headers={"X-Admin-Key": ADMIN_KEY}
                )
            except Exception:
                pass

        exp_str = "Навсегда" if expires is None else expires.strftime("%d.%m.%Y")
        await m.answer(
            f"✅ <b>Ключ активирован!</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
            f"📅 Подписка до: <b>{exp_str}</b>",
            parse_mode="HTML", reply_markup=self._main_kb(True)
        )
        await state.clear()

    # Back
    async def _cb_back(self, c: CallbackQuery, state: FSMContext):
        await state.clear()
        acc = await self.db.get_account_by_user_id(c.from_user.id)
        await c.message.edit_text(
            f"👋 Привет! Чем могу помочь?",
            reply_markup=self._main_kb(acc is not None)
        )
        await c.answer()

    # /help
    async def _help(self, m: Message):
        await m.answer(
            "<b>📖 Помощь</b>\n\n"
            "/start — главное меню\n"
            "/profile — профиль и подписка\n"
            "/login — данные для лоадера\n\n"
            "По вопросам: @support",
            parse_mode="HTML"
        )

    # ======================== ADMIN ========================

    def _check_admin(self, user_id: int) -> bool:
        return ADMIN_ID and user_id == ADMIN_ID

    async def _admin(self, m: Message):
        if not self._check_admin(m.from_user.id):
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton(text="📊 Статистика",   callback_data="admin_stats")],
            [InlineKeyboardButton(text="🔑 Генерация ключей", callback_data="admin_keys")],
        ])
        await m.answer("⚙️ <b>Панель администратора</b>", parse_mode="HTML", reply_markup=kb)

    async def _cb_admin_stats(self, c: CallbackQuery):
        if not self._check_admin(c.from_user.id):
            return
        stats = await self.db.get_stats()
        await c.message.edit_text(
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего аккаунтов: <b>{stats['total']}</b>\n"
            f"💎 Платных: <b>{stats['paid']}</b>\n"
            f"💰 Выручка: <b>{stats['revenue']:.2f} USDT</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
            ])
        )
        await c.answer()

    async def _cb_admin_users(self, c: CallbackQuery):
        if not self._check_admin(c.from_user.id):
            return
        accounts = await self.db.get_all_accounts()
        lines = []
        for a in accounts[:30]:
            exp = a.get("subscription_expires")
            status = "✅" if a["is_paid"] and (exp is None or (isinstance(exp, datetime) and exp > datetime.utcnow())) else "❌"
            lines.append(f"{status} <code>{a['login']}</code>")
        text = "<b>👥 Аккаунты</b>\n\n" + ("\n".join(lines) if lines else "Пусто")
        await c.message.edit_text(text, parse_mode="HTML", reply_markup=self._back_kb())
        await c.answer()

    async def _delete(self, m: Message):
        if not self._check_admin(m.from_user.id):
            return
        parts = m.text.split()
        if len(parts) < 2:
            await m.answer("Используй: /delete <user_id>")
            return
        try:
            uid = int(parts[1])
        except ValueError:
            await m.answer("❌ Неверный user_id")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Удалить", callback_data=f"confirm_delete_{uid}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")],
        ])
        await m.answer(f"Удалить пользователя <code>{uid}</code>?", parse_mode="HTML", reply_markup=kb)

    async def _cb_confirm_delete(self, c: CallbackQuery):
        if not self._check_admin(c.from_user.id):
            return
        uid = int(c.data.replace("confirm_delete_", ""))
        ok = await self.db.delete_account(uid)
        await c.message.edit_text(
            f"{'✅ Удалён' if ok else '❌ Не найден'}: <code>{uid}</code>",
            parse_mode="HTML"
        )
        await c.answer()

    async def _cb_admin_keys(self, c: CallbackQuery):
        if not self._check_admin(c.from_user.id):
            return
        await c.message.edit_text(
            "🔑 <b>Генерация ключей</b>\nВыбери тариф:",
            parse_mode="HTML",
            reply_markup=self._plans_kb("admin_key_")
        )
        await c.answer()

    async def _cb_admin_key_plan(self, c: CallbackQuery, state: FSMContext):
        if not self._check_admin(c.from_user.id):
            return
        plan_id = c.data.replace("admin_key_", "")
        if plan_id not in SUBSCRIPTION_PLANS:
            await c.answer("❌ Неверный тариф")
            return
        await state.update_data(plan_id=plan_id)
        await state.set_state(AdminKeyState.waiting_for_count)
        await c.message.edit_text(f"Сколько ключей создать для <b>{SUBSCRIPTION_PLANS[plan_id]['title']}</b>? (1–50)",
                                   parse_mode="HTML")
        await c.answer()

    async def _admin_key_count_message(self, m: Message, state: FSMContext):
        if not self._check_admin(m.from_user.id):
            return
        try:
            count = int(m.text.strip())
        except ValueError:
            await m.answer("❌ Введи число от 1 до 50:")
            return
        if not (1 <= count <= 50):
            await m.answer("❌ Количество должно быть от 1 до 50:")
            return

        data = await state.get_data()
        plan_id = data.get("plan_id")
        plan = SUBSCRIPTION_PLANS.get(plan_id)
        if not plan:
            await state.clear()
            await m.answer("❌ Тариф не найден.")
            return

        keys = [await self.db.create_subscription_key(plan_id, m.from_user.id) for _ in range(count)]
        await state.clear()

        keys_text = "\n".join(f"<code>{k}</code>" for k in keys)
        await m.answer(
            f"✅ <b>Ключи созданы</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
            f"Количество: <b>{count}</b>\n\n{keys_text}",
            parse_mode="HTML"
        )

    # ---------- Run ----------
    async def run(self):
        await self.db.init()
        os.makedirs("files", exist_ok=True)
        try:
            await start_dummy_healthcheck_server()
        except Exception as e:
            print(f"⚠️ Healthcheck server warning: {e}")
        print(f"✅ {PRODUCT_NAME} bot started (PostgreSQL)")
        await self.dp.start_polling(self.bot)

    async def shutdown(self):
        await self.crypto.close()
        await self.db.close()


from aiohttp import web

async def start_dummy_healthcheck_server():
    """Dummy web server so Render Free Web Service stays healthy and 100% FREE."""
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Free Web Service healthcheck listening on port {port}")


async def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set"); return
    if not DATABASE_URL:
        print("❌ DATABASE_URL not set"); return
    if not CRYPTO_PAY_API_KEY:
        print("❌ CRYPTO_PAY_API_KEY not set"); return
    bot = OyuzBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        print("\n👋 Bye")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
