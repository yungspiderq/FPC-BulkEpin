"""Bulk Epin supplier integration for sidor0912/FunPayCardinal, Python 3.10+."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

NAME = "Bulk Epin"
VERSION = "1.0.0"
DESCRIPTION = "Автовыдача Bulk Epin: Steam, Telegram, игры, ключи и карты. /bulk"
CREDITS = "https://github.com/yungspiderq"
UUID = "758e01f9-a143-4870-b0ad-158c7701e5a5"
SETTINGS_PAGE = True
ROOT = Path("storage/plugins/bulkepin")
BASE_URL = "https://bulkepin.com/api/public/v1"
LOG = logging.getLogger("FPC.BulkEpin")
KINDS = {"steam-topup": "Пополнение Steam", "steam-gift": "Игра Steam в подарок",
         "telegram-stars": "Telegram Stars", "telegram-premium": "Telegram Premium",
         "topups": "Пополнение игры", "giftcards": "Подарочная карта",
         "gamekeys": "Ключ игры", "manual": "Другой сервис"}
NAMES = {"steamLogin": "Логин Steam (не ник)", "invite_url": "Ссылка приглашения Steam",
         "telegram_username": "Username Telegram", "player_id": "ID игрока",
         "server_id": "ID сервера"}
DEFAULT = {"enabled": False, "dry_run": True, "api_key": "", "price_risk_accepted": False,
           "min_balance": "0", "lots": {}, "account_id": None}
REMOTE_STATES = {"pending", "processing", "completed", "failed", "cancelled", "refunded"}
ACTIVE_STATES = {"pending", "completed", "ready"}


class PluginError(Exception):
    """Only safe, locally generated messages; no credentials or supplier response bodies."""


class APIError(PluginError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def number(value, zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not zero and result == 0):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise PluginError("Нужно положительное число" + (" или ноль." if zero else ".")) from None


def integer(value, maximum=100000):
    n = number(value)
    if n != n.to_integral_value() or n > maximum:
        raise PluginError(f"Нужно целое число от 1 до {maximum}.")
    return int(n)


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def validate_rule(rule):
    r = copy.deepcopy(rule)
    if not isinstance(r, dict) or r.get("kind") not in KINDS:
        raise PluginError("Неизвестный тип товара.")
    if not isinstance(r.get("params"), dict) or not str(r.get("product_key", "")).strip():
        raise PluginError("Нужны product_key и параметры товара.")
    if type(r.get("enabled", True)) is not bool:
        raise PluginError("Некорректный переключатель лота.")
    r["product_key"] = str(r["product_key"])
    if len(r["product_key"]) > 200:
        raise PluginError("Слишком длинный идентификатор товара.")
    r["max_cost"] = str(number(r.get("max_cost")))
    r["max_units"] = integer(r.get("max_units", 1), 100)
    r["quantity"] = integer(r.get("quantity", 1), 100)
    kind, p = r["kind"], r["params"]
    if kind not in {"giftcards", "gamekeys"} and (r["quantity"] != 1 or r["max_units"] != 1):
        raise PluginError("Для пополнений, подписок и услуг разрешена одна штука лота за заказ.")
    required = {"topups": ["category_id", "offer_id"], "giftcards": ["category_id", "card_id"],
                "gamekeys": ["game_id", "key_id"], "steam-gift": ["app_id", "sub_id", "region"],
                "manual": ["manual_service_id", "product_id"]}.get(kind, [])
    for key in required:
        if not isinstance(p.get(key), (str, int)) or not str(p[key]).strip():
            raise PluginError(f"Не задан параметр {key}.")
        p[key] = str(p[key])
    if kind == "steam-topup":
        p["amount"] = float(number(p.get("amount")))
        if p["amount"] > 100000 or not re.fullmatch(r"[A-Z]{3}", str(p.get("currency", ""))):
            raise PluginError("Проверьте сумму и валюту Steam.")
    if kind == "telegram-stars":
        p["quantity"] = integer(p.get("quantity"), 10000)
        if p["quantity"] < 50:
            raise PluginError("Минимум 50 Stars.")
    if kind == "telegram-premium":
        p["months"] = integer(p.get("months"), 12)
        if p["months"] not in {3, 6, 12}:
            raise PluginError("Premium: 3, 6 или 12 месяцев.")
    inputs = r.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > 10:
        raise PluginError("Допускается до 10 полей покупателя.")
    paths = []
    for field in inputs:
        if (not isinstance(field, dict) or not isinstance(field.get("path"), str)
                or not re.fullmatch(r"(?:fields\.)?[A-Za-z0-9_-]{1,64}", field["path"])):
            raise PluginError("Неверное поле покупателя.")
        path = field["path"]
        if re.search(r"password|passwd|secret|token|cookie|otp|2fa", path, re.I):
            raise PluginError("Услуги с паролями, токенами или кодами входа не поддерживаются.")
        if path in paths or path in {"amount", "quantity", "months", "region", *required}:
            raise PluginError("Покупатель не может изменять выбранный товар или количество.")
        allowed = {"steam-topup": ["steamLogin"], "steam-gift": ["invite_url"],
                   "telegram-stars": ["telegram_username"],
                   "telegram-premium": ["telegram_username"]}.get(kind)
        if (allowed is not None and path not in allowed) or (
                allowed is None and (kind not in {"topups", "manual"} or not path.startswith("fields."))):
            raise PluginError("Поле покупателя не соответствует виду товара.")
        paths.append(path)
    expected = {"steam-topup": "steamLogin", "steam-gift": "invite_url",
                "telegram-stars": "telegram_username", "telegram-premium": "telegram_username"}.get(kind)
    if expected and paths != [expected]:
        raise PluginError("Получатель должен запрашиваться у покупателя.")
    r["inputs"] = inputs
    return r


def recipient_value(field, text):
    value = str(text).strip()
    if not value or len(value) > 300 or any(ord(c) < 32 for c in value):
        raise PluginError("Пустое или слишком длинное значение.")
    path = field["path"]
    if path == "steamLogin" and not re.fullmatch(r"[A-Za-z0-9_]{2,64}", value):
        raise PluginError("Нужен логин Steam, не ссылка и не имя профиля.")
    if path == "telegram_username":
        value = value.lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
            raise PluginError("Нужен username Telegram.")
    if path == "invite_url":
        url = urlparse(value)
        if (url.scheme != "https" or url.hostname not in {"s.team", "steamcommunity.com"}
                or url.username or url.password or url.fragment
                or not (url.path.startswith(("/p/", "/f/", "/user/")))):
            raise PluginError("Нужна HTTPS-ссылка приглашения Steam (s.team или steamcommunity.com).")
    if field.get("choices") and value not in [str(i) for i in field["choices"]]:
        raise PluginError("Выберите одно из значений: " + ", ".join(map(str, field["choices"])))
    return value


def make_payload(rule, units, values):
    r = validate_rule(rule)
    units = integer(units, r["max_units"])
    p = copy.deepcopy(r["params"])
    for f in r["inputs"]:
        value = recipient_value(f, values.get(f["path"], ""))
        if f["path"].startswith("fields."):
            p.setdefault("fields", {})[f["path"].split(".", 1)[1]] = value
        else:
            p[f["path"]] = value
    quantity = integer(r["quantity"] * units, 100)
    return {"kind": r["kind"], "product_key": r["product_key"], "quantity": quantity, "params": p}


class Config:
    def __init__(self, root):
        self.path = Path(root) / "config.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        data = copy.deepcopy(DEFAULT)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise PluginError("Повреждён config.json.")
            data.update(saved)
        self.save(data)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self, data):
        for key in ("enabled", "dry_run", "price_risk_accepted"):
            if type(data.get(key)) is not bool:
                raise PluginError("Неверные переключатели в настройках.")
        number(data.get("min_balance"), zero=True)
        if not isinstance(data.get("api_key"), str) or not isinstance(data.get("lots"), dict):
            raise PluginError("Неверный формат настроек.")
        for lot, rule in data["lots"].items():
            integer(lot, 10**15)
            validate_rule(rule)
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, self.path)
            self.data = copy.deepcopy(data)


class Ledger:
    def __init__(self, root):
        self.path = Path(root) / "orders.db"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, state TEXT, body TEXT)")
        for row in self.rows():
            if row["state"] in {"creating", "delivering"}:
                row["state"] = "uncertain" if row["state"] == "creating" else "delivery_review"
                self.put(row)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def add(self, row):
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?)",
                              (row["id"], row["state"], json.dumps(row))).rowcount == 1

    def put(self, row):
        row["updated"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE orders SET state=?,body=? WHERE id=?",
                       (row["state"], json.dumps(row, ensure_ascii=False), row["id"]))

    def get(self, oid):
        with self.connect() as db:
            result = db.execute("SELECT body FROM orders WHERE id=?", (str(oid),)).fetchone()
        return json.loads(result[0]) if result else None

    def rows(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM orders ORDER BY rowid DESC")]


class InstanceLock:
    def __init__(self, root):
        Path(root).mkdir(parents=True, exist_ok=True)
        self.stream = (Path(root) / "instance.lock").open("a+b")
        self.stream.seek(0)
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise PluginError("Это хранилище уже использует другой процесс.") from None

    def close(self):
        self.stream.close()


class API:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.next_call = 0.0
        self.cooldown = 0.0

    def key(self):
        key = os.environ.get("BULKEPIN_API_KEY") or self.config.snapshot()["api_key"]
        if not key or any(c.isspace() for c in key):
            raise PluginError("Укажите API-ключ в /bulk → API-ключ.")
        return key

    def call(self, endpoint, payload=None, params=None):
        key = self.key()
        if time.monotonic() < self.cooldown:
            raise APIError("Лимит Bulk Epin: дождитесь окончания паузы.", 429)
        time.sleep(max(0, self.next_call - time.monotonic()))
        self.next_call = time.monotonic() + 1.1
        try:
            r = self.session.request("GET" if payload is None else "POST", BASE_URL + endpoint,
                                     params=params, json=payload, headers={"Authorization": "Bearer " + key},
                                     timeout=(10, 40), allow_redirects=False)
        except requests.RequestException:
            raise APIError("Сеть/таймаут Bulk Epin. Результат покупки может быть неизвестен.") from None
        if r.status_code == 429:
            try:
                delay = max(60, float(r.headers.get("Retry-After", 60)),
                            float(r.json().get("retry_after_seconds", 60)))
                if not 0 < delay <= 86400:
                    delay = 3600
            except (TypeError, ValueError, AttributeError):
                delay = 60
            self.cooldown = time.monotonic() + delay
        if r.status_code not in (200, 201):
            raise APIError(f"Bulk Epin HTTP {r.status_code}. Проверьте ключ, баланс и статус заказа.", r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise APIError("Некорректный JSON Bulk Epin.") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise APIError("Bulk Epin не подтвердил успешный ответ.")
        return data


def authorized(c, obj):
    return bool(c.telegram and obj and obj.chat.type == "private"
                and obj.from_user.id in c.telegram.authorized_users)


class Engine:
    def __init__(self, cardinal, root=ROOT, api=None):
        self.c = cardinal
        self.instance = InstanceLock(root)
        try:
            self.config = Config(root)
            self.ledger = Ledger(root)
        except Exception:
            self.instance.close()
            raise
        self.api = api or API(self.config)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="BulkEpin")
        self.stopped = False
        self.paused = threading.Event()
        self.exit = threading.Event()
        self.tick_pending = False
        self.tick_lock = threading.Lock()
        self.thread = None
        self.lot_cache = {}
        self.panel = Panel(self)

    def active(self):
        plugin = self.c.plugins.get(UUID)
        return (not self.stopped and not self.paused.is_set() and bool(plugin and plugin.enabled)
                and self.config.snapshot()["enabled"])

    def bind_account(self):
        cfg = self.config.snapshot()
        owner = str(self.c.account.id)
        if not owner.isdigit() or int(owner) <= 0:
            raise PluginError("FunPay ещё не авторизован.")
        if cfg["account_id"] is None:
            cfg["account_id"] = owner
            self.config.save(cfg)
        elif str(cfg["account_id"]) != owner:
            raise PluginError("Хранилище принадлежит другому аккаунту FunPay.")

    def submit(self, fn, *args):
        if self.stopped:
            return
        def run():
            with self.lock:
                if self.stopped:
                    return
                try:
                    fn(*args)
                except PluginError as exc:
                    self.notify(str(exc))
                except Exception:
                    LOG.error("Bulk Epin: ошибка задачи; проверьте журнал /bulk. Секреты не записываются.")
                    self.notify("Ошибка задачи. Проверьте журнал /bulk; автоматической повторной покупки нет.")
        return self.pool.submit(run)

    def notify(self, text):
        if self.c.telegram:
            try:
                self.c.telegram.send_notification("<b>Bulk Epin</b>\n" + html.escape(text))
            except Exception:
                LOG.warning("Bulk Epin: уведомление не отправлено.")

    def tell(self, row, text):
        try:
            return bool(self.c.send_message(row["chat_id"], text, row["buyer_name"]))
        except Exception:
            return False

    def phase(self, row, state, **changes):
        row.update(state=state, **changes)
        self.ledger.put(row)

    def refresh_lots(self):
        fresh = self.c.account.get_user(self.c.account.id)
        if fresh.id != self.c.account.id or self.c.profile is None:
            raise PluginError("Не удалось загрузить профиль FunPay.")
        lots = fresh.get_sorted_lots(1)
        if not isinstance(lots, dict):
            raise PluginError("Неверный список лотов FunPay.")
        for lot in lots.values():
            self.c.profile.update_lot(lot)
        self.lot_cache = dict(lots)
        return lots

    def verify_order(self, row):
        self.bind_account()
        o = self.c.account.get_order(row["id"])
        if (o.id != row["id"] or getattr(o.status, "name", "") != "PAID" or o.seller_id != self.c.account.id
                or o.buyer_id != row["buyer_id"] or str(o.chat_id) != str(row["chat_id"])
                or o.short_description != row["description"] or o.amount != row["units"]
                or getattr(o, "order_secrets", None)):
            raise PluginError("Заказ FunPay не оплачен, изменён, возвращён или уже выдан самим FunPay.")
        if getattr(self.c, "bl_delivery_enabled", False) and row["buyer_name"] in self.c.blacklist:
            raise PluginError("Покупатель в чёрном списке Cardinal.")
        return o

    def new_order(self, event):
        if not self.active() or getattr(self.c, "old_mode_enabled", False):
            return
        shortcut = event.order
        if not re.fullmatch(r"[A-Z0-9]+", shortcut.id) or shortcut.id == "ADTEST" or self.ledger.get(shortcut.id):
            return
        lid = str(getattr(event, "lot_id", ""))
        cfg = self.config.snapshot()
        rule = cfg["lots"].get(lid)
        if not rule or not rule.get("enabled", True):
            return
        native = getattr(event, "config_section_obj", None)
        if getattr(event, "delivered", False) or (native is not None and not native.getboolean("disable")):
            self.notify(f"#{shortcut.id}: отключите встроенную автовыдачу Cardinal у лота {lid}.")
            return
        try:
            lot = getattr(event, "lot_shortcut", None)
            o = self.c.account.get_order(shortcut.id)
            if not lot or getattr(lot, "auto", False) or o.buyer_id != shortcut.buyer_id:
                raise PluginError("Не удалось проверить лот или включена автовыдача FunPay.")
            candidates = [candidate for candidate in self.c.profile.get_sorted_lots(1).values()
                          if candidate.description == o.short_description
                          and getattr(candidate.subcategory, "id", None) == getattr(o.subcategory, "id", None)
                          and (candidate.server or "") == (getattr(o.server, "name", None) or "")
                          and (candidate.side or "") == (getattr(o.side, "name", None) or "")]
            if len(candidates) != 1 or str(candidates[0].id) != lid:
                raise PluginError("Неоднозначный лот. Используйте уникальные названия в категории/сервере.")
            rule = validate_rule(rule)
            units = integer(o.amount, rule["max_units"])
            row = {"id": o.id, "state": "collecting", "lot_id": lid, "rule": rule,
                   "units": units, "description": o.short_description, "buyer_id": o.buyer_id,
                   "buyer_name": o.buyer_username, "chat_id": o.chat_id, "values": {},
                   "created": time.time(), "dry_run": cfg["dry_run"], "key_hash": fingerprint(self.api.key()),
                   "idempotency_key": "fpc-" + str(self.c.account.id) + "-" + o.id + "-" + uuid.uuid4().hex,
                   "last_poll": 0, "error": ""}
            self.verify_order(row)
            if not self.ledger.add(row):
                return
            if rule["inputs"]:
                self.ask(row)
            else:
                self.phase(row, "ready")
                self.process(row)
        except PluginError as exc:
            self.notify(f"#{shortcut.id}: {exc}")

    def ask(self, row):
        missing = [f for f in row["rule"]["inputs"] if f["path"] not in row["values"]]
        if missing:
            field = missing[0]
            title = field.get("label") or NAMES.get(field["path"], field["path"])
            if field.get("choices"):
                title += " (варианты: " + ", ".join(map(str, field["choices"])) + ")"
            self.tell(row, f"Заказ #{row['id']}. Укажите: {title}.\n"
                      f"Отправьте: !bulk {row['id']} ЗНАЧЕНИЕ\nПароль от аккаунта не нужен.")
        else:
            self.phase(row, "confirm", nonce=secrets.token_hex(3), confirm_until=time.time() + 900)
            info = "; ".join(row["values"].values())
            self.tell(row, f"Заказ #{row['id']}. Проверьте получателя: {info}.\n"
                      f"Подтвердите: !bulk {row['id']} да {row['nonce']}\n"
                      f"Для исправления: !bulk {row['id']} заново")

    def message(self, msg):
        if not self.active() or getattr(msg, "by_bot", False) or getattr(msg, "badge", None):
            return
        parts = (getattr(msg, "text", None) or "").strip().split(maxsplit=2)
        if len(parts) != 3 or parts[0].lower() != "!bulk":
            return
        row = self.ledger.get(parts[1].lstrip("#").upper())
        if (not row or row["state"] not in {"collecting", "confirm"}
                or msg.author_id != row["buyer_id"] or str(msg.chat_id) != str(row["chat_id"])):
            return
        try:
            self.verify_order(row)
            value = parts[2].strip()
            if value.lower() == "заново":
                self.phase(row, "collecting", values={})
                self.ask(row)
            elif row["state"] == "confirm":
                if value != "да " + row["nonce"] or time.time() > row["confirm_until"]:
                    raise PluginError("Неверное/устаревшее подтверждение. Отправьте !bulk НОМЕР_ЗАКАЗА заново.")
                self.phase(row, "ready")
                self.process(row)
            else:
                field = next(f for f in row["rule"]["inputs"] if f["path"] not in row["values"])
                row["values"][field["path"]] = recipient_value(field, value)
                self.ledger.put(row)
                self.ask(row)
        except PluginError as exc:
            self.tell(row, str(exc))

    def same_key(self, row):
        if fingerprint(self.api.key()) != row["key_hash"]:
            raise PluginError("API-ключ изменился. Верните прежний ключ для обработки этого заказа.")

    def process(self, row):
        if row["state"] != "ready" or not self.active():
            return
        try:
            self.same_key(row)
            self.verify_order(row)
            payload = make_payload(row["rule"], row["units"], row["values"])
            q = self.api.call("/quote", payload)["quote"]
            if (q.get("kind") != payload["kind"] or str(q.get("product_key")) != payload["product_key"]
                    or q.get("quantity") != payload["quantity"]):
                raise PluginError("Расчёт поставщика не соответствует заказу.")
            total = number(q.get("total_usd"))
            if total > number(row["rule"]["max_cost"]):
                raise PluginError("Предварительная цена превышает лимит лота.")
            cfg = self.config.snapshot()
            if q.get("can_pay") is not True or number(q.get("balance"), zero=True) - total < number(cfg["min_balance"], zero=True):
                raise PluginError("Недостаточно баланса или будет нарушен минимальный остаток.")
            row["quoted_usd"] = str(total)
            if row["dry_run"] or cfg["dry_run"]:
                self.phase(row, "dry_run")
                self.notify(f"#{row['id']}: тест пройден, расчёт {total} USD. Покупка НЕ выполнена.")
                return
            if not cfg["price_risk_accepted"]:
                raise PluginError("Подтвердите условия реального режима в /bulk.")
            self.verify_order(row)
            if not self.active():
                return
            payload["idempotency_key"] = row["idempotency_key"]
            # Freeze the exact request BEFORE spending. Never rebuild it for a retry.
            self.phase(row, "creating", payload=payload)
            self.create(row)
        except PluginError as exc:
            if row["state"] == "ready":
                self.phase(row, "blocked", error=str(exc))
                self.notify(f"#{row['id']}: {exc}")

    def create(self, row):
        try:
            self.same_key(row)
            if not self.active() or self.config.snapshot()["dry_run"] or row["dry_run"]:
                raise PluginError("Новые покупки остановлены.")
            data = self.api.call("/orders", copy.deepcopy(row["payload"]))
            order = data.get("order", {})
            if not isinstance(order.get("id"), str) or not order["id"] or order.get("status") not in REMOTE_STATES:
                raise PluginError("Не удалось подтвердить номер покупки Bulk Epin.")
            if order.get("kind") != row["rule"]["kind"] or order.get("quantity") != row["payload"]["quantity"]:
                raise PluginError("Ответ покупки не соответствует запросу.")
            self.phase(row, "pending", remote_id=order["id"], last_poll=0, error="")
            self.poll(row)
        except Exception as exc:
            error = str(exc) if isinstance(exc, PluginError) else "Не удалось разобрать ответ поставщика."
            if not row.get("remote_id"):
                self.phase(row, "uncertain", error=error)
                self.notify(f"#{row['id']}: результат покупки неизвестен. Проверьте журнал /bulk.")
            else:
                row["error"] = error
                self.ledger.put(row)

    def poll(self, row):
        self.same_key(row)
        row["last_poll"] = time.time()
        self.ledger.put(row)
        o = self.api.call("/orders/" + quote(row["remote_id"], safe="")).get("order", {})
        if (o.get("id") != row["remote_id"] or o.get("kind") != row["rule"]["kind"]
                or str(o.get("product_key")) != row["payload"]["product_key"]
                or o.get("quantity") != row["payload"]["quantity"] or o.get("status") not in REMOTE_STATES):
            raise PluginError("Несоответствие данных заказа Bulk Epin; выдача остановлена.")
        status = o["status"]
        if status in {"failed", "cancelled", "refunded"}:
            self.phase(row, "failed", remote_status=status)
            self.notify(f"#{row['id']}: статус поставщика {status}. Проверьте баланс Bulk Epin и заказ FunPay; возврат FunPay не выполнялся.")
        elif status == "completed":
            total = number(o.get("total_usd"), zero=True)
            self.phase(row, "completed", remote_status=status, cost=str(total),
                       codes=o.get("codes", []), delivery=o.get("delivery"))
            if total > number(row["rule"]["max_cost"]):
                self.notify(f"#{row['id']}: фактическая цена {total} USD выше предварительного лимита. Товар уже куплен.")
            self.deliver(row)
        else:
            row["remote_status"] = status
            row["error"] = ""
            if time.time() - row["created"] > 1800 and not row.get("slow_notice"):
                row["slow_notice"] = True
                self.notify(f"#{row['id']}: поставщик обрабатывает заказ больше 30 минут. Новая покупка не создаётся.")
            self.ledger.put(row)

    def deliver(self, row):
        try:
            self.verify_order(row)
            codes = row.get("codes")
            if not isinstance(codes, list) or any(not isinstance(s, str) for s in codes):
                raise PluginError("Неверный формат выданных кодов.")
            if codes:
                normalized = [code.strip() for code in codes]
                if not all(normalized) or len(set(normalized)) != len(normalized):
                    raise PluginError("Поставщик вернул пустые или повторяющиеся коды. Нужна сверка.")
                if row["rule"]["kind"] in {"giftcards", "gamekeys"}:
                    used = {code.strip() for other in self.ledger.rows() if other["id"] != row["id"]
                            for code in (other.get("codes") or []) if isinstance(code, str)}
                    if used.intersection(normalized):
                        raise PluginError("Этот код уже встречался в другом заказе. Автовыдача остановлена.")
                content = "\n".join(codes)
            elif row.get("delivery"):
                content = row["delivery"] if isinstance(row["delivery"], str) else json.dumps(row["delivery"], ensure_ascii=False)
            elif row["rule"]["kind"] in {"giftcards", "gamekeys", "manual"}:
                raise PluginError("Поставщик завершил заказ без данных для выдачи. Нужна ручная проверка.")
            else:
                content = "Поставщик подтвердил выполнение заказа. Проверьте пополнение или получение подарка."
            if len(content) > 40000:
                raise PluginError("Слишком большой ответ для автоматической выдачи.")
            chunks = row.get("chunks") or [f"Заказ #{row['id']} — результат:\n" + content[i:i+2500]
                                            for i in range(0, len(content), 2500)]
            self.phase(row, "completed", chunks=chunks)
            for idx in range(row.get("sent", 0), len(chunks)):
                self.phase(row, "delivering", sent=idx)
                if not self.tell(row, chunks[idx]):
                    raise PluginError("Доставка сообщения не подтверждена. Проверьте чат перед повтором.")
                self.phase(row, "completed", sent=idx + 1)
            self.phase(row, "done", error="")
            self.notify(f"#{row['id']}: выполнен, {row.get('cost', '?')} USD.")
        except PluginError as exc:
            self.phase(row, "delivery_review", error=str(exc))
            self.notify(f"#{row['id']}: товар куплен, но выдача требует проверки. {exc}")

    def tick(self):
        if not self.active():
            return
        candidates = [r for r in self.ledger.rows() if r["state"] in ACTIVE_STATES
                      and time.time() - r.get("last_poll", 0) >= (60 if r.get("slow_notice") else 10)]
        if not candidates:
            return
        row = min(candidates, key=lambda r: r.get("last_poll", 0))
        try:
            if row["state"] == "pending":
                self.poll(row)
            elif row["state"] == "completed":
                self.deliver(row)
            else:
                self.process(row)
        except PluginError as exc:
            if row.get("error") != str(exc):
                self.notify(f"#{row['id']}: {exc}")
            row.update(error=str(exc), last_poll=time.time())
            self.ledger.put(row)

    def start(self):
        if self.thread:
            return
        def loop():
            while not self.exit.wait(2):
                with self.tick_lock:
                    if self.tick_pending or self.stopped:
                        continue
                    self.tick_pending = True
                def task():
                    try:
                        self.tick()
                    finally:
                        with self.tick_lock:
                            self.tick_pending = False
                self.submit(task)
        self.thread = threading.Thread(target=loop, name="BulkEpin-poller", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped = True
        self.exit.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.instance.close()


def offer_rule(kind, product, offer, response):
    """Offer IDs are dynamic: only use returned fields, never hard-coded SKU IDs."""
    if not isinstance(offer, dict):
        raise PluginError("Неверный формат предложения.")
    r = {"enabled": True, "kind": kind, "product_key": str(product), "params": {},
         "quantity": 1, "max_units": 1, "inputs": []}
    mapping = {"topups": ("category_id", "offer_id"), "giftcards": ("category_id", "card_id"),
               "gamekeys": ("game_id", "key_id"), "manual": ("manual_service_id", "product_id")}
    if kind in mapping:
        parent, child = mapping[kind]
        value = offer.get(child, offer.get("id"))
        if value is None or isinstance(value, (dict, list, bool)):
            raise PluginError("В предложении нет ID. Используйте настройку JSON по документации поставщика.")
        r["params"] = {parent: str(product), child: str(value)}
    elif kind == "steam-gift":
        if not offer.get("sub_id") or not offer.get("region"):
            raise PluginError("У Steam-гифта не указан sub_id или регион.")
        r["params"] = {"app_id": str(product), "sub_id": str(offer["sub_id"]), "region": str(offer["region"])}
        r["inputs"] = [{"path": "invite_url", "label": NAMES["invite_url"]}]
    if kind in {"topups", "manual"}:
        definitions = {}
        for src in (response, offer):
            ui = src.get("ui") or {}
            for f in src.get("fields", []) + ui.get("fields", []):
                if isinstance(f, dict) and f.get("key"):
                    definitions[str(f["key"])] = f
        if not definitions:
            raise PluginError("API не прислал поля получателя. Используйте JSON-настройку с точными полями из кабинета.")
        r["params"]["fields"] = {}
        for key, f in definitions.items():
            if f.get("required", True) is False:
                continue
            item = {"path": "fields." + key, "label": str(f.get("label") or key)}
            values = f.get("enum", f.get("options"))
            if isinstance(values, list) and values:
                choices = [v.get("value") if isinstance(v, dict) else v for v in values]
                if all(isinstance(v, (str, int)) for v in choices):
                    item["choices"] = [str(v) for v in choices]
            r["inputs"].append(item)
    return r


STATES = {"collecting": "Ждём данные покупателя", "confirm": "Ждём подтверждение",
          "ready": "Готов к обработке", "creating": "Запрос покупки", "uncertain": "Покупка требует сверки",
          "pending": "Обрабатывается поставщиком", "completed": "Куплен, ожидает выдачи",
          "delivering": "Отправка сообщения", "delivery_review": "Проверьте выдачу в чате",
          "done": "Выдан", "dry_run": "Тест: покупки не было", "blocked": "Остановлен до покупки",
          "failed": "Не выполнен поставщиком", "cancelled": "Отменён до покупки"}


def rule_summary(rule):
    p = rule["params"]
    kind = rule["kind"]
    if kind == "steam-topup":
        return f"Покупатель получит на Steam: {number(p['amount']):g} {p['currency']}"
    if kind == "telegram-stars":
        return f"Покупатель получит: {p['quantity']} Stars"
    if kind == "telegram-premium":
        return f"Покупатель получит Premium на {p['months']} мес."
    if kind == "steam-gift":
        return f"Игра Steam {p['app_id']}, издание {p['sub_id']}, регион {p['region']}"
    if kind in {"giftcards", "gamekeys"}:
        item = p.get("card_id", p.get("key_id"))
        return f"Покупатель получит кодов: {rule.get('quantity', 1)} · товар {item}"
    return f"Услуга: {p.get('offer_id', p.get('product_id'))} · товар {rule['product_key']}"


class Panel:
    def __init__(self, engine):
        self.e = engine
        self.sessions = {}
        self.pending = {}
        self.drafts = {}
        self.choices = {}

    @staticmethod
    def context(message, user=None, edit=False):
        return {"chat": message.chat.id, "user": user or message.from_user.id,
                "mid": message.id if edit else None}

    @staticmethod
    def key(ctx):
        return ctx["chat"], ctx["user"]

    def show(self, ctx, text, rows=()):
        from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
        token = secrets.token_hex(4)
        keyboard = InlineKeyboardMarkup()
        for row in rows:
            buttons = []
            for label, action in row:
                data = action[1:] if action.startswith("=") else f"bulk:{token}:{action}"
                if len(data.encode()) > 64:
                    raise PluginError("Слишком длинный идентификатор кнопки.")
                buttons.append(InlineKeyboardButton(label, callback_data=data))
            keyboard.row(*buttons)
        bot = self.e.c.telegram.bot
        body = html.escape(text[:3800])
        if ctx.get("mid"):
            try:
                bot.edit_message_text(body, ctx["chat"], ctx["mid"], reply_markup=keyboard, parse_mode="HTML")
            except Exception:
                sent = bot.send_message(ctx["chat"], body, reply_markup=keyboard, parse_mode="HTML")
                ctx["mid"] = sent.message_id
        else:
            sent = bot.send_message(ctx["chat"], body, reply_markup=keyboard, parse_mode="HTML")
            ctx["mid"] = sent.message_id
        self.sessions[self.key(ctx)] = {"token": token, "mid": ctx["mid"]}

    def home(self, ctx, note=""):
        self.pending.pop(self.key(ctx), None)
        cfg = self.e.config.snapshot()
        from tg_bot import CBT
        self.show(ctx, f"🛒 Bulk Epin v{VERSION}\n\n"
                  f"Плагин: {'🟢 ВКЛ' if cfg['enabled'] else '🔴 ВЫКЛ'}\n"
                  f"Режим: {'🧪 тест — деньги не списываются' if cfg['dry_run'] else '💳 реальные покупки'}\n"
                  f"Лотов настроено: {len(cfg['lots'])}\n"
                  f"Минимальный остаток: {cfg['min_balance']} USD\n\n{note}",
                  [[("🔴 Выключить" if cfg["enabled"] else "🟢 Включить", "toggle")],
                   [("🧪 Тест / реальные покупки", "mode")], [("🔑 API-ключ", "key"), ("💰 Баланс", "balance")],
                   [("📦 Мои лоты", "lots:0"), ("🛍 Каталог API", "catalog")],
                   [("🧾 Заказы", "orders:0"), ("🛡 Минимальный остаток", "reserve")],
                   [("❓ Как настроить", "help"), ("🔄 Обновить меню", "home")],
                   [("◀️ К плагину", f"={CBT.EDIT_PLUGIN}:{UUID}:0")]])

    def begin(self, ctx, step, prompt, **data):
        self.pending[self.key(ctx)] = {"step": step, "data": data, "expires": time.time()+900}
        self.show(ctx, prompt, [[("❌ Отмена", "home")]])

    def has_input(self, message):
        if not authorized(self.e.c, message) or (message.text or "").startswith("/"):
            return False
        p = self.pending.get((message.chat.id, message.from_user.id))
        return bool(p and p["expires"] >= time.time())

    def lots(self, ctx, page=0):
        lots = list(self.e.refresh_lots().values())
        cfg = self.e.config.snapshot()
        rows = [[(f"{'✅' if str(lot.id) in cfg['lots'] else '➕'} {lot.id} · {lot.description[:30]}", f"lot:{lot.id}")]
                for lot in lots[page*8:page*8+8]]
        # Include configured inactive lots: their settings must remain reachable.
        missing = [lid for lid in cfg["lots"] if int(lid) not in self.e.lot_cache]
        if page == 0:
            rows += [[(f"⚪ Неактивный лот {lid}", f"lot:{lid}")] for lid in missing[:8]]
        if page:
            rows.append([("⬅️", f"lots:{page-1}")])
        if len(lots) > page*8+8:
            rows.append([("➡️", f"lots:{page+1}")])
        rows += [[("🔄 Обновить с FunPay", "lots:0")], [("🏠 Меню", "home")]]
        self.show(ctx, "📦 Лоты FunPay\nСвежий список с сайта. Новые лоты доступны без перезапуска.\n"
                  "Перед настройкой отключите у лота автовыдачу FunPay и Cardinal.", rows)

    def lot(self, ctx, lid):
        r = self.e.config.snapshot()["lots"].get(lid)
        text = f"📦 Лот {lid}\n"
        if r:
            text += (f"{KINDS[r['kind']]}\n{'✅ Включён' if r.get('enabled', True) else '⛔ Выключен'}\n"
                     f"За 1 штуку лота:\n{rule_summary(r)}\n"
                     f"Порог предварительной цены всего заказа: {r['max_cost']} USD\n"
                     f"Максимум штук лота в заказе: {r['max_units']}\n")
        else:
            text += "Не настроен."
        rows = [[("⚙️ Настроить по шагам", f"setup:{lid}")], [("📝 Настроить JSON", f"json:{lid}")]]
        if r:
            rows += [[("✅ / ⛔ Переключить", f"switch:{lid}")], [("🗑 Убрать привязку", f"unbind:{lid}")]]
        self.show(ctx, text, rows + [[("◀️ Лоты", "lots:0")]])

    def select_kind(self, ctx, lid):
        self.drafts[self.key(ctx)] = {"lot_id": lid}
        self.show(ctx, "Что выдаёт этот лот?", [[(name, f"kind:{kind}")] for kind, name in KINDS.items()]
                  + [[("❌ Отмена", "home")]])

    def products(self, ctx, search="", page=0):
        d = self.drafts[self.key(ctx)]
        kind = d["kind"]
        result = self.e.api.call("/products", params={"kind": kind, "search": search, "limit": 8, "offset": page*8})
        items = result.get("items", [])
        if not isinstance(items, list):
            raise PluginError("Не удалось прочитать каталог.")
        self.choices[self.key(ctx)] = items
        d.update(search=search, page=page)
        rows = [[(str(item.get("name") or item.get("title") or item.get("id") or f"Товар {i+1}")[:55], f"product:{i}")]
                for i, item in enumerate(items) if isinstance(item, dict)]
        rows += [[("🔎 Поиск", "search"), ("🔢 Ввести ID", "productid")]]
        if page:
            rows.append([("⬅️", f"products:{page-1}")])
        if result.get("total", 0) > page*8+8:
            rows.append([("➡️", f"products:{page+1}")])
        self.show(ctx, f"🛍 {KINDS[kind]}\nВыберите товар. Регион и номинал выбираются на следующем шаге.",
                  rows + [[("❌ Отмена", "home")]])

    def offers(self, ctx, product):
        d = self.drafts[self.key(ctx)]
        response = self.e.api.call("/offers", params={"kind": d["kind"], "id": product})
        d.update(product=str(product), offers=response)
        offers = response.get("offers", [])
        if not isinstance(offers, list) or not offers:
            raise PluginError("Нет доступных предложений. Проверьте ID, наличие и доступ в кабинете Bulk Epin.")
        self.show_offers(ctx, 0)

    def show_offers(self, ctx, page):
        offers = self.drafts[self.key(ctx)]["offers"]["offers"]
        rows = []
        for i, offer in enumerate(offers[page*8:page*8+8], start=page*8):
            title = offer.get("name") or offer.get("title") or offer.get("id") or offer.get("sub_id") or str(i+1)
            label = f"{title} · {offer.get('region', '')} · {offer.get('price_usd', '?')} USD"
            rows.append([(label[:60], f"offer:{i}")])
        if page:
            rows.append([("⬅️", f"offers:{page-1}")])
        if len(offers) > page*8+8:
            rows.append([("➡️", f"offers:{page+1}")])
        self.show(ctx, "Выберите регион / номинал / издание.\nЦены ориентировочные; перед покупкой выполняется новый расчёт.",
                  rows + [[("❌ Отмена", "home")]])

    def cost_prompt(self, ctx):
        self.begin(ctx, "cost", "🛡 Введите максимальную предварительную стоимость ОДНОГО ЗАКАЗА в USD.\n"
                   "Например: 5.50\n\nЭто закупочная цена, а не цена продажи на FunPay. "
                   "Bulk Epin пересчитывает цену при покупке и не предоставляет жёсткий предел списания.")

    def preview(self, ctx):
        d = self.drafts[self.key(ctx)]
        r = validate_rule(d["rule"])
        self.show(ctx, f"Проверьте привязку лота {d['lot_id']}\n\n{KINDS[r['kind']]}\n"
                  f"За 1 штуку лота:\n{rule_summary(r)}\n"
                  f"Порог предварительной цены заказа: {r['max_cost']} USD\n"
                  f"Максимум штук в заказе: {r['max_units']}\n"
                  f"Данные от покупателя: {', '.join(f.get('label', f['path']) for f in r['inputs']) or 'не нужны'}\n\n"
                  "Проверьте регион товара. Сохранение не выполняет покупку.",
                  [[("💾 Сохранить", "save")], [("❌ Отмена", "home")]])

    def input(self, message):
        if not self.has_input(message):
            return
        ctx = self.context(message)
        p = self.pending[self.key(ctx)]
        text = message.text.strip()
        step = p["step"]
        try:
            cfg = self.e.config.snapshot()
            d = self.drafts.get(self.key(ctx), {})
            if step == "key":
                if len(text) < 8 or len(text) > 1024 or any(c.isspace() for c in text):
                    raise PluginError("Неверный формат API-ключа.")
                cfg["api_key"] = text
                self.e.config.save(cfg)
                try:
                    self.e.c.telegram.bot.delete_message(message.chat.id, message.id)
                except Exception:
                    pass
                self.pending.pop(self.key(ctx), None)
                self.e.api.call("/ping")
                self.home(ctx, "✅ Ключ сохранён, проверка API пройдена. Значение ключа не показывается.")
            elif step == "reserve":
                cfg["min_balance"] = str(number(text.replace(",", "."), zero=True))
                self.e.config.save(cfg)
                self.home(ctx, "Порог сохранён. Он блокирует новые покупки, но не скрывает лоты FunPay.")
            elif step == "search":
                self.pending.pop(self.key(ctx), None)
                self.products(ctx, text)
            elif step == "productid":
                self.pending.pop(self.key(ctx), None)
                self.offers(ctx, text)
            elif step in {"steam", "stars", "premium"}:
                kind = d["kind"]
                r = {"kind": kind, "product_key": kind, "quantity": 1, "max_units": 1, "enabled": True}
                if step == "steam":
                    pair = text.split()
                    if len(pair) != 2:
                        raise PluginError("Введите сумму и валюту через пробел: 100 RUB или 5 USD.")
                    r["params"] = {"amount": float(number(pair[0].replace(",", "."))), "currency": pair[1].upper()}
                    path = "steamLogin"
                elif step == "stars":
                    r["params"] = {"quantity": integer(text, 10000)}
                    if r["params"]["quantity"] < 50:
                        raise PluginError("Минимум 50 Stars.")
                    path = "telegram_username"
                else:
                    months = integer(text, 12)
                    if months not in {3, 6, 12}:
                        raise PluginError("Выберите 3, 6 или 12 месяцев.")
                    r["params"] = {"months": months}
                    path = "telegram_username"
                r["inputs"] = [{"path": path, "label": NAMES[path]}]
                d["rule"] = r
                self.cost_prompt(ctx)
            elif step == "cost":
                d["rule"]["max_cost"] = str(number(text.replace(",", ".")))
                if d["kind"] in {"gamekeys", "giftcards"}:
                    self.begin(ctx, "units", "Сколько штук лота покупатель может купить одним заказом?\n"
                               "Введите число 1–100. Одна штука лота = один код/карта. Для безопасного старта: 1.")
                else:
                    self.pending.pop(self.key(ctx), None)
                    self.preview(ctx)
            elif step == "units":
                d["rule"]["max_units"] = integer(text, 100)
                self.pending.pop(self.key(ctx), None)
                self.preview(ctx)
            elif step == "json":
                r = validate_rule(json.loads(text))
                self.drafts[self.key(ctx)] = {"lot_id": p["data"]["lot_id"], "kind": r["kind"], "rule": r}
                self.pending.pop(self.key(ctx), None)
                self.preview(ctx)
        except (PluginError, ValueError, TypeError):
            # Do not echo malformed input: it may be an API key or account data.
            self.show(ctx, "Не удалось выполнить шаг. Проверьте формат и повторите ввод. "
                      "Для API-ключа также проверьте доступ к Bulk Epin.\nЗначение не выводится из соображений безопасности.",
                      [[("🏠 Меню", "home")]])

    def orders(self, ctx, page=0):
        rows = self.e.ledger.rows()
        buttons = [[(f"#{r['id']} · {STATES.get(r['state'], r['state'])}"[:60], f"order:{r['id']}")]
                   for r in rows[page*8:page*8+8]]
        if page:
            buttons.append([("⬅️", f"orders:{page-1}")])
        if len(rows) > page*8+8:
            buttons.append([("➡️", f"orders:{page+1}")])
        self.show(ctx, "🧾 Журнал заказов\nКоды и данные покупателей в Telegram не выводятся.",
                  buttons + [[("🏠 Меню", "home")]])

    def order(self, ctx, oid):
        row = self.e.ledger.get(oid)
        if not row:
            raise PluginError("Заказ не найден.")
        buttons = []
        if row["state"] == "blocked":
            buttons.append([("🔄 Повторить проверку", f"retry:{oid}")])
        if row["state"] == "uncertain":
            buttons.append([("🔎 Восстановить запрос", f"recoverask:{oid}")])
        if row["state"] == "pending":
            buttons.append([("🔄 Проверить статус", f"poll:{oid}")])
        if row["state"] == "delivery_review":
            buttons.append([("💬 Повторить выдачу", f"resendask:{oid}")])
        if row["state"] in {"collecting", "confirm"}:
            buttons.append([("💬 Повторить инструкцию", f"prompt:{oid}")])
        self.show(ctx, f"Заказ #{oid}\n{STATES.get(row['state'], row['state'])}\n"
                  f"Лот: {row['lot_id']}\nBulk Epin: {row.get('remote_id', 'ещё не известен')}\n"
                  f"Расчёт: {row.get('quoted_usd', '—')} USD\nСписано: {row.get('cost', '—')} USD\n"
                  f"{row.get('error', '')}", buttons + [[("◀️ Заказы", "orders:0")]])

    def callback(self, call):
        if not call.message or not authorized(self.e.c, type("Sender", (), {"chat": call.message.chat, "from_user": call.from_user})()):
            return
        ctx = self.context(call.message, call.from_user.id, True)
        parts = call.data.split(":", 2)
        session = self.sessions.get(self.key(ctx), {})
        if len(parts) != 3 or session.get("token") != parts[1] or session.get("mid") != call.message.id:
            return
        self.pending.pop(self.key(ctx), None)
        try:
            self.route(ctx, parts[2])
        except PluginError as exc:
            self.show(ctx, str(exc), [[("🏠 Меню", "home")]])
        except Exception:
            self.show(ctx, "Операция не завершена. Проверьте доступ к API и формат каталога. Покупки из меню настройки не выполняются.",
                      [[("🏠 Меню", "home")]])

    def route(self, ctx, action):
        name, _, arg = action.partition(":")
        cfg = self.e.config.snapshot()
        key = self.key(ctx)
        if name == "home":
            self.home(ctx)
        elif name == "toggle":
            self.e.bind_account()
            cfg = self.e.config.snapshot()
            cfg["enabled"] = not cfg["enabled"]
            self.e.config.save(cfg)
            self.e.paused.clear() if cfg["enabled"] else self.e.paused.set()
            self.home(ctx)
        elif name == "mode":
            self.show(ctx, "Режим работы\n\n🧪 Тест: проверка заказа и расчёт цены, без покупки и выдачи.\n"
                      "💳 Реальный: деньги списываются с Bulk Epin.\n"
                      "Расчёт /quote не фиксирует цену. API покупки не принимает жёсткий предел списания. "
                      "Порог в настройках проверяется до покупки; цена может измениться.\n"
                      "Тестовые заказы никогда не становятся реальными автоматически.",
                      [[("🧪 Включить тест", "dry")], [("💳 Принимаю условия, реальный режим", "live")], [("🏠 Меню", "home")]])
        elif name in {"dry", "live"}:
            cfg["dry_run"] = name == "dry"
            cfg["price_risk_accepted"] = name == "live"
            self.e.config.save(cfg)
            self.home(ctx)
        elif name == "key":
            self.begin(ctx, "key", "🔑 Отправьте API-ключ Bulk Epin следующим сообщением.\n"
                       "Сообщение с ключом будет удалено, если Telegram разрешит. Ключ сохранится только локально.\n"
                       "Альтернатива: переменная окружения BULKEPIN_API_KEY (приоритетнее настройки).")
        elif name == "balance":
            b = self.e.api.call("/balance")
            amount = number(b.get("balance"), zero=True)
            if b.get("currency") != "USD":
                raise PluginError("Неожиданная валюта баланса.")
            self.show(ctx, f"💰 Баланс Bulk Epin: {amount:.4f} USD\nПополнение выполняется в кабинете поставщика.",
                      [[("🏠 Меню", "home")]])
        elif name == "reserve":
            self.begin(ctx, "reserve", "Минимальный остаток USD после покупки.\nВведите 0 для отключения.\n"
                       "Это запрет новых закупок, а не отключение лотов на FunPay.")
        elif name == "catalog":
            data = self.e.api.call("/catalog")
            self.show(ctx, "🛍 Доступные виды товаров\n" + "\n".join(KINDS.get(k, k) for k in data.get("product_kinds", []))
                      + "\n\nДля выбора товара откройте Мои лоты → лот → Настроить по шагам.",
                      [[("📦 Мои лоты", "lots:0")], [("🏠 Меню", "home")]])
        elif name == "lots":
            self.lots(ctx, int(arg))
        elif name == "lot":
            self.lot(ctx, arg)
        elif name == "setup":
            if int(arg) not in self.e.refresh_lots():
                raise PluginError("Сначала опубликуйте лот FunPay и обновите список.")
            fields = self.e.c.account.get_lot_fields(int(arg))
            if getattr(fields, "auto_delivery", False):
                raise PluginError("Сначала отключите встроенную автовыдачу FunPay у этого лота.")
            self.select_kind(ctx, arg)
        elif name == "kind":
            d = self.drafts[key]
            d["kind"] = arg
            if arg in {"steam-topup", "telegram-stars", "telegram-premium"}:
                self.e.api.call("/offers", params={"kind": arg})
                step, prompt = {"steam-topup": ("steam", "Сколько получает покупатель за ОДНУ штуку лота?\n"
                                               "Введите сумму и валюту: 100 RUB — за 1 штуку лота пополним Steam на 100 рублей."),
                                "telegram-stars": ("stars", "Сколько Stars за одну штуку лота? Введите 50–10000."),
                                "telegram-premium": ("premium", "Срок Premium за одну штуку лота: 3, 6 или 12 месяцев.")}[arg]
                self.begin(ctx, step, prompt)
            else:
                self.products(ctx)
        elif name == "search":
            self.begin(ctx, "search", "Введите название игры/карты для поиска.")
        elif name == "productid":
            self.begin(ctx, "productid", "Введите ID товара из Bulk Epin (для Steam Gift — app_id игры).")
        elif name == "products":
            self.products(ctx, self.drafts[key].get("search", ""), int(arg))
        elif name == "product":
            item = self.choices[key][int(arg)]
            kind = self.drafts[key]["kind"]
            field = {"topups": "category_id", "giftcards": "category_id", "gamekeys": "game_id",
                     "steam-gift": "app_id", "manual": "manual_service_id"}[kind]
            value = item.get(field, item.get("id"))
            if value is None:
                raise PluginError("Каталог не содержит ожидаемый ID. Введите ID из кабинета вручную.")
            self.offers(ctx, str(value))
        elif name == "offers":
            self.show_offers(ctx, int(arg))
        elif name == "offer":
            d = self.drafts[key]
            d["rule"] = offer_rule(d["kind"], d["product"], d["offers"]["offers"][int(arg)], d["offers"])
            self.cost_prompt(ctx)
        elif name == "save":
            d = self.drafts[key]
            if int(d["lot_id"]) not in self.e.refresh_lots():
                raise PluginError("Лот больше не виден в профиле. Проверьте публикацию.")
            fields = self.e.c.account.get_lot_fields(int(d["lot_id"]))
            if getattr(fields, "auto_delivery", False):
                raise PluginError("Отключите выдачу самого FunPay.")
            cfg["lots"][d["lot_id"]] = validate_rule(d["rule"])
            self.e.config.save(cfg)
            self.lot(ctx, d["lot_id"])
        elif name == "json":
            self.begin(ctx, "json", "Вставьте JSON привязки из примеров README.\n"
                       "Поля: kind, product_key, params, inputs, max_cost, max_units.\n"
                       "Используйте реальные ID из API. Ключ API сюда вставлять не нужно.", lot_id=arg)
        elif name == "switch":
            cfg["lots"][arg]["enabled"] = not cfg["lots"][arg].get("enabled", True)
            self.e.config.save(cfg)
            self.lot(ctx, arg)
        elif name == "unbind":
            cfg["lots"].pop(arg, None)
            self.e.config.save(cfg)
            self.lot(ctx, arg)
        elif name == "orders":
            self.orders(ctx, int(arg))
        elif name == "order":
            self.order(ctx, arg)
        elif name in {"recoverask", "resendask"}:
            text = ("Восстановить неизвестный запрос покупки?\nСначала проверьте заказ в кабинете Bulk Epin. "
                    "Будет отправлено сохранённое тело с ТЕМ ЖЕ ключом идемпотентности. "
                    "Если первый запрос не дошёл, это выполнит покупку. Не используйте после ручной закупки/возврата."
                    if name == "recoverask" else "Проверьте чат покупателя. Повтор может продублировать сообщение, "
                    "если FunPay принял его, но ответ потерялся. Повторной покупки у поставщика не будет.")
            self.show(ctx, text, [[("✅ Подтверждаю", f"{'recover' if name == 'recoverask' else 'resend'}:{arg}")],
                                  [("❌ Отмена", f"order:{arg}")]])
        elif name in {"retry", "recover", "resend", "poll", "prompt"}:
            row = self.e.ledger.get(arg)
            if not row or not self.e.active():
                raise PluginError("Включите плагин, чтобы обработать заказ.")
            if name == "retry" and row["state"] == "blocked":
                self.e.phase(row, "ready", error="")
                self.e.process(row)
            elif name == "recover" and row["state"] == "uncertain":
                if cfg["dry_run"] or row["dry_run"] or not cfg["price_risk_accepted"]:
                    raise PluginError("В тестовом режиме запрос покупки запрещён.")
                self.e.verify_order(row)
                self.e.same_key(row)
                self.e.phase(row, "creating")
                self.e.create(row)
            elif name == "resend" and row["state"] == "delivery_review":
                self.e.deliver(row)
            elif name == "poll" and row["state"] == "pending":
                self.e.poll(row)
            elif name == "prompt" and row["state"] in {"collecting", "confirm"}:
                self.e.ask(row)
            self.order(ctx, arg)
        elif name == "help":
            self.show(ctx, "1. Укажите ключ Bulk Epin.\n2. Откройте Мои лоты → Настроить по шагам.\n"
                      "3. Отключите штатную автовыдачу FunPay и Cardinal у этих лотов.\n"
                      "4. Проверьте тестовый режим на заказе. Тест не выполняет выдачу!\n"
                      "5. Включите реальный режим и плагин.\n\n"
                      "При необходимости покупатель получит инструкции !bulk и подтвердит реквизиты. "
                      "Ключи/карты выдаются после completed. Возвраты денег FunPay выполняются вручную.\n"
                      "Не удаляйте orders.db: это защита от повторных покупок.", [[("🏠 Меню", "home")]])


_engine = None


def init(cardinal, *args):
    global _engine
    if _engine is not None:
        return
    try:
        _engine = Engine(cardinal)
    except Exception:
        LOG.error("Bulk Epin: ошибка конфигурации/блокировки/журнала. Плагин не запущен, данные не сброшены.")
        return
    if cardinal.telegram:
        cardinal.add_telegram_commands(UUID, [("bulk", "Bulk Epin: автовыдача", True)])
        from tg_bot import CBT

        def command(message):
            if authorized(cardinal, message):
                if message.text.split()[0].split("@")[0] == "/bulk_off":
                    _engine.paused.set()
                    def disable():
                        cfg = _engine.config.snapshot()
                        cfg["enabled"] = False
                        _engine.config.save(cfg)
                        _engine.panel.home(_engine.panel.context(message))
                    _engine.submit(disable)
                else:
                    _engine.submit(_engine.panel.home, _engine.panel.context(message))

        def callback(call):
            if (not call.message or call.message.chat.type != "private"
                    or call.from_user.id not in cardinal.telegram.authorized_users):
                return
            try:
                cardinal.telegram.bot.answer_callback_query(call.id)
            except Exception:
                pass
            session = _engine.panel.sessions.get((call.message.chat.id, call.from_user.id), {})
            parts = call.data.split(":", 2)
            if (len(parts) == 3 and parts[1] == session.get("token") and call.message.id == session.get("mid")
                    and parts[2] == "toggle" and _engine.config.snapshot()["enabled"]):
                _engine.paused.set()
            if call.data.startswith("bulk:"):
                _engine.submit(_engine.panel.callback, call)
            else:
                _engine.submit(_engine.panel.home, _engine.panel.context(call.message, call.from_user.id, True))

        cardinal.telegram.msg_handler(command, commands=["bulk", "bulk_off"])
        cardinal.telegram.cbq_handler(callback, lambda c: bool(c.data) and (c.data.startswith("bulk:")
                                      or c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}")))
        cardinal.telegram.msg_handler(lambda m: _engine.submit(_engine.panel.input, m),
                                      func=_engine.panel.has_input, content_types=["text"])
    LOG.info("Bulk Epin %s initialized", VERSION)


def new_order(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.new_order, event)


def new_message(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.message, event.message)


def start(cardinal, *args):
    if _engine:
        _engine.start()


def stop(*args):
    if _engine and not _engine.stopped:
        _engine.stopped = True
        _engine.exit.set()
        # Retain instance lock until in-flight work has finished.
        threading.Thread(target=_engine.close, name="BulkEpin-shutdown", daemon=True).start()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_PRE_STOP = [stop]
BIND_TO_DELETE = stop
