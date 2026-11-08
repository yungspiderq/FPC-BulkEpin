import copy
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import requests

spec = importlib.util.spec_from_file_location("bulkepin", Path(__file__).parents[1] / "bulkepin.py")
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


def rule(kind="giftcards"):
    params = {
        "giftcards": {"category_id": "cat", "card_id": "card"},
        "gamekeys": {"game_id": "game", "key_id": "key"},
        "topups": {"category_id": "game", "offer_id": "pack", "fields": {}},
        "manual": {"manual_service_id": "service", "product_id": "product", "fields": {}},
        "steam-topup": {"amount": 100, "currency": "RUB"},
        "steam-gift": {"app_id": "123", "sub_id": "456", "region": "RU"},
        "telegram-stars": {"quantity": 50},
        "telegram-premium": {"months": 3},
    }[kind]
    path = {"topups": "fields.player_id", "manual": "fields.player_id", "steam-topup": "steamLogin",
            "steam-gift": "invite_url", "telegram-stars": "telegram_username",
            "telegram-premium": "telegram_username"}.get(kind)
    return {"kind": kind, "product_key": kind if kind in {"steam-topup", "telegram-stars", "telegram-premium"}
            else next(iter(params.values())), "params": params, "max_cost": "10", "max_units": 1,
            "quantity": 1, "inputs": [{"path": path}] if path else [], "enabled": True}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.status = "pending"
        self.total = 2
        self.balance = 100
        self.key_value = "TEST-KEY-NOT-REAL"
        self.fail_create = False
        self.fail_poll = False
        self.on_create = None

    def key(self):
        return self.key_value

    def call(self, path, payload=None, params=None):
        self.calls.append((path, copy.deepcopy(payload), copy.deepcopy(params)))
        if path == "/quote":
            return {"ok": True, "quote": {**{k: payload[k] for k in ("kind", "product_key", "quantity")},
                    "total_usd": self.total, "balance": self.balance, "can_pay": self.balance >= self.total}}
        if path == "/orders":
            key = payload["idempotency_key"]
            self.orders.setdefault(key, {"id": "remote-" + str(len(self.orders)), "kind": payload["kind"],
                                        "product_key": payload["product_key"], "quantity": payload["quantity"],
                                        "status": "pending", "total_usd": self.total, "codes": []})
            if self.on_create:
                self.on_create()
            if self.fail_create:
                raise p.APIError("Сеть/таймаут")
            return {"ok": True, "order": copy.deepcopy(self.orders[key])}
        if path.startswith("/orders/"):
            if self.fail_poll:
                raise p.APIError("Сбой чтения", 503)
            row = next(copy.deepcopy(o) for o in self.orders.values() if path.endswith(o["id"]))
            row.update(status=self.status, codes=["SECRET-CODE-A"] if self.status == "completed" else [], delivery=None)
            return {"ok": True, "order": row}
        if path == "/balance":
            return {"ok": True, "balance": self.balance, "currency": "USD"}
        if path == "/catalog":
            return {"ok": True, "product_kinds": list(p.KINDS)}
        if path == "/products":
            return {"ok": True, "total": 1, "items": [{"id": "cat", "name": "Test card"}]}
        if path == "/offers":
            return {"ok": True, "offers": [{"id": "card", "name": "USD card", "price_usd": 2}]}
        return {"ok": True}


class Bot:
    def __init__(self):
        self.messages = []
        self.deleted = []

    def send_message(self, chat, text, **kwargs):
        mid = len(self.messages) + 1
        self.messages.append(NS(chat=chat, text=text, mid=mid, **kwargs))
        return NS(message_id=mid)

    def edit_message_text(self, text, chat, mid, **kwargs):
        self.messages.append(NS(chat=chat, text=text, mid=mid, **kwargs))

    def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("BULKEPIN_API_KEY", raising=False)
    tg_bot = ModuleType("tg_bot")
    tg_bot.CBT = NS(EDIT_PLUGIN="45", PLUGIN_SETTINGS="47")
    monkeypatch.setitem(sys.modules, "tg_bot", tg_bot)
    cat = NS(id=3)
    lot = NS(id=123, description="Unique card", subcategory=cat, server=None, side=None, auto=False)
    lots = {123: lot}
    profile = NS(get_sorted_lots=lambda _: lots, update_lot=lambda item: lots.update({item.id: item}))
    full = NS(id="ORDER1", short_description=lot.description, subcategory=cat, server=None, side=None,
              buyer_id=5, buyer_username="buyer", seller_id=9, status=NS(name="PAID"), chat_id=10,
              amount=1, order_secrets=[])
    notifications, sent = [], []
    c = NS(account=NS(id=9, get_order=lambda _: full,
                      get_user=lambda _: NS(id=9, get_sorted_lots=lambda _: lots),
                      get_lot_fields=lambda _: NS(auto_delivery=False)),
           profile=profile, plugins={p.UUID: NS(enabled=True)}, blacklist=[], bl_delivery_enabled=True,
           old_mode_enabled=False, telegram=NS(bot=Bot(), authorized_users=[1], send_notification=notifications.append))
    c.send_message = lambda chat, text, buyer: sent.append((chat, text, buyer)) or True
    api = FakeAPI()
    e = p.Engine(c, tmp_path, api)
    cfg = e.config.snapshot()
    cfg.update(enabled=True, dry_run=False, price_risk_accepted=True, lots={"123": rule()})
    e.config.save(cfg)
    event = NS(order=NS(id="ORDER1", buyer_id=5), lot_id=123, lot_shortcut=lot, delivered=False, config_section_obj=None)
    yield NS(e=e, c=c, api=api, full=full, event=event, sent=sent, notifications=notifications, root=tmp_path, lots=lots)
    e.close()


@pytest.mark.parametrize("kind", list(p.KINDS))
def test_all_documented_product_kinds_build_payloads(kind):
    r = rule(kind)
    values = {f["path"]: ("https://s.team/p/abc/def" if f["path"] == "invite_url" else "player123") for f in r["inputs"]}
    body = p.make_payload(r, 1, values)
    assert body["kind"] == kind and body["quantity"] == 1
    assert body["product_key"] == r["product_key"]
    assert "idempotency_key" not in body


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", True, None, "nonsense"])
def test_bad_money_rejected(value):
    with pytest.raises(p.PluginError):
        p.number(value)


@pytest.mark.parametrize("kind", ["steam-topup", "steam-gift", "telegram-stars", "telegram-premium", "topups", "manual"])
def test_service_does_not_silently_ignore_funpay_quantity(kind):
    r = rule(kind)
    r["max_units"] = 2
    with pytest.raises(p.PluginError):
        p.validate_rule(r)


@pytest.mark.parametrize("path", ["amount", "quantity", "region", "card_id", "fields.password", "fields.api_token"])
def test_buyer_cannot_choose_price_product_or_secrets(path):
    r = rule("topups")
    r["inputs"] = [{"path": path}]
    with pytest.raises(p.PluginError):
        p.validate_rule(r)


def test_duplicate_event_and_pending_response_do_not_deliver(setup):
    s = setup
    s.e.new_order(s.event)
    s.e.new_order(s.event)
    assert len(s.api.orders) == 1
    assert not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "pending"
    assert sum(path == "/orders" for path, _, _ in s.api.calls) == 1


def test_completed_order_delivered_once(setup):
    s = setup
    s.api.status = "completed"
    s.e.new_order(s.event)
    s.e.new_order(s.event)
    s.e.tick()
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    assert len(s.sent) == 1 and "SECRET-CODE-A" in s.sent[0][1]
    assert not any("SECRET-CODE-A" in n for n in s.notifications)


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "cancelled", "refunded"])
def test_non_completed_status_never_delivers(setup, status):
    setup.api.status = status
    setup.e.new_order(setup.event)
    assert not setup.sent
    assert setup.e.ledger.get("ORDER1")["state"] == ("pending" if status in {"pending", "processing"} else "failed")


def test_timeout_saved_body_and_key_survive_restart(setup):
    s = setup
    s.api.fail_create = True
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "uncertain" and len(s.api.orders) == 1
    payload = copy.deepcopy(row["payload"])
    s.e.close()
    other = p.Engine(s.c, s.root, s.api)
    try:
        other.tick()  # never automatically buy again after an uncertain POST
        assert len([c for c in s.api.calls if c[0] == "/orders"]) == 1
        s.api.fail_create = False
        other.create(other.ledger.get("ORDER1"))
        assert len(s.api.orders) == 1
        assert [b for path, b, _ in s.api.calls if path == "/orders"] == [payload, payload]
    finally:
        other.close()


def test_disk_record_exists_before_paid_request(setup):
    s = setup
    def inspect():
        row = p.Ledger(s.root).get("ORDER1")
        assert row["payload"]["idempotency_key"] in s.api.orders
        assert row["state"] == "uncertain"  # simulated restart turns creating into uncertain
    s.api.on_create = inspect
    s.e.new_order(s.event)


def test_pending_orders_resume_polling_after_restart(setup):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    row["last_poll"] = 0
    s.e.ledger.put(row)
    s.e.close()
    other = p.Engine(s.c, s.root, s.api)
    try:
        s.api.status = "completed"
        other.tick()
        assert other.ledger.get("ORDER1")["state"] == "done"
        assert len(s.api.orders) == 1 and len(s.sent) == 1
    finally:
        other.close()


def test_delivery_ambiguity_requires_manual_retry_without_buying(setup):
    s = setup
    s.api.status = "completed"
    s.c.send_message = lambda *args: False
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"
    s.e.tick()
    assert len(s.api.orders) == 1
    s.c.send_message = lambda *args: True
    s.e.deliver(s.e.ledger.get("ORDER1"))
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    assert len(s.api.orders) == 1


def test_dry_run_never_buys_and_cannot_be_promoted(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["dry_run"] = True
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "dry_run"
    cfg["dry_run"] = False
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    s.e.tick()
    assert not s.api.orders and not s.sent


@pytest.mark.parametrize("reason", ["price", "funds", "reserve", "risk", "refunded", "seller", "quantity", "blacklist", "secrets"])
def test_payment_guards(setup, reason):
    s = setup
    cfg = s.e.config.snapshot()
    if reason == "price":
        s.api.total = 11
    elif reason == "funds":
        s.api.balance = 1
    elif reason == "reserve":
        cfg["min_balance"] = "99"
    elif reason == "risk":
        cfg["price_risk_accepted"] = False
    elif reason == "refunded":
        s.full.status.name = "REFUNDED"
    elif reason == "seller":
        s.full.seller_id = 88
    elif reason == "quantity":
        s.full.amount = 2
    elif reason == "blacklist":
        s.c.blacklist = ["buyer"]
    elif reason == "secrets":
        s.full.order_secrets = ["native"]
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    assert not s.api.orders


def test_native_cardinal_delivery_is_not_duplicated(setup):
    setup.event.delivered = True
    setup.e.new_order(setup.event)
    assert not setup.api.calls


def test_ambiguous_lot_is_not_guessed(setup):
    setup.lots[124] = copy.copy(setup.lots[123])
    setup.lots[124].id = 124
    setup.e.new_order(setup.event)
    assert not setup.api.orders


def test_buyer_identity_and_confirmation(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steam-topup")
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    wrong = NS(text="!bulk ORDER1 player123", chat_id=10, author_id=6)
    s.e.message(wrong)
    assert s.e.ledger.get("ORDER1")["values"] == {}
    right = NS(text="!bulk ORDER1 player123", chat_id=10, author_id=5)
    s.e.message(right)
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "confirm" and not s.api.orders
    right.text = "!bulk ORDER1 да wrong"
    s.e.message(right)
    assert not s.api.orders
    right.text = "!bulk ORDER1 да " + row["nonce"]
    s.e.message(right)
    assert len(s.api.orders) == 1
    payload = next(b for path, b, _ in s.api.calls if path == "/orders")
    assert payload["params"]["amount"] == 100 and payload["params"]["steamLogin"] == "player123"


def test_api_key_change_stops_recovery(setup):
    s = setup
    s.api.fail_create = True
    s.e.new_order(s.event)
    s.api.key_value = "OTHER-ACCOUNT"
    s.api.fail_create = False
    s.e.create(s.e.ledger.get("ORDER1"))
    assert len([c for c in s.api.calls if c[0] == "/orders"]) == 1


def test_dynamic_topup_fields_use_exact_names():
    response = {"fields": [{"key": "userId", "label": "Player ID", "type": "text", "required": True}]}
    offer = {"offer_id": "live-offer", "ui": {"fields": [{"key": "zone", "label": "Zone", "required": True}]}}
    r = p.offer_rule("topups", "live-game", offer, response)
    r["max_cost"] = "10"
    body = p.make_payload(r, 1, {"fields.userId": "00123", "fields.zone": "EU"})
    assert body["params"] == {"category_id": "live-game", "offer_id": "live-offer", "fields": {"userId": "00123", "zone": "EU"}}


def test_missing_topup_fields_stops_wizard():
    with pytest.raises(p.PluginError):
        p.offer_rule("topups", "game", {"id": "offer"}, {})


def test_corrupt_settings_do_not_reset_to_defaults(tmp_path):
    (tmp_path / "config.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError):
        p.Config(tmp_path)
    assert (tmp_path / "config.json").read_text() == "{bad"


def test_atomic_write_failure_preserves_config(setup, monkeypatch):
    cfg = setup.e.config.snapshot()
    cfg["enabled"] = False
    monkeypatch.setattr(p.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        setup.e.config.save(cfg)
    assert setup.e.config.snapshot()["enabled"] is True
    assert json.loads((setup.root / "config.json").read_text())["enabled"] is True


def test_second_instance_rejected(setup):
    with pytest.raises(p.PluginError):
        p.Engine(setup.c, setup.root, setup.api)


def test_refund_while_supplier_processes_stops_delivery(setup):
    s = setup
    s.api.status = "completed"
    s.api.on_create = lambda: setattr(s.full.status, "name", "REFUNDED")
    s.e.new_order(s.event)
    assert len(s.api.orders) == 1
    assert not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"


@pytest.mark.parametrize("codes", [[], [""], [" "], ["SAME", "SAME"], [None]])
def test_missing_or_duplicate_codes_require_review(setup, codes):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    row.update(codes=codes, delivery=None)
    s.e.deliver(row)
    assert not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"


def test_same_supplier_code_in_second_order_is_not_delivered(setup):
    s = setup
    s.api.status = "completed"
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    s.full.id = "ORDER2"
    s.event.order.id = "ORDER2"
    s.e.new_order(s.event)
    assert len(s.api.orders) == 2  # Provider incorrectly reused a code across distinct purchases.
    assert len(s.sent) == 1
    assert s.e.ledger.get("ORDER2")["state"] == "delivery_review"


def test_crash_during_message_send_requires_review_after_restart(setup):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    s.e.phase(row, "delivering", codes=["CODE"], sent=0)
    s.e.close()
    other = p.Engine(s.c, s.root, s.api)
    try:
        assert other.ledger.get("ORDER1")["state"] == "delivery_review"
        other.tick()
        assert not s.sent and len(s.api.orders) == 1
    finally:
        other.close()


def test_pause_during_quote_prevents_purchase(setup):
    s = setup
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == "/quote":
            s.e.paused.set()
        return result
    s.api.call = call
    s.e.new_order(s.event)
    assert not s.api.orders


def test_account_change_prevents_purchase(setup):
    s = setup
    s.e.bind_account()
    s.c.account.id = 999
    s.full.seller_id = 999
    s.e.new_order(s.event)
    assert not s.api.orders


@pytest.mark.parametrize("bad", [{}, {"order": None}, {"order": {"id": "other", "kind": "wrong", "status": "completed"}}])
def test_malformed_paid_response_is_not_retried(setup, bad):
    s = setup
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return bad if path == "/orders" else result
    s.api.call = call
    s.e.new_order(s.event)
    s.e.tick()
    assert s.e.ledger.get("ORDER1")["state"] == "uncertain"
    assert not s.sent and len(s.api.orders) == 1


def test_multiple_giftcards_use_total_quantity_and_total_price(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"].update(max_units=3, quantity=2)
    s.e.config.save(cfg)
    s.full.amount = 3
    s.e.new_order(s.event)
    body = next(body for path, body, _ in s.api.calls if path == "/orders")
    assert body["quantity"] == 6


def test_quote_mismatched_product_prevents_spending(setup):
    s = setup
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == "/quote":
            result["quote"]["product_key"] = "wrong-product"
        return result
    s.api.call = call
    s.e.new_order(s.event)
    assert not s.api.orders
    assert s.e.ledger.get("ORDER1")["state"] == "blocked"


@pytest.mark.parametrize("code", [200, 201, 401, 402, 403, 422, 429, 500, 302])
def test_http_contract_no_hidden_retries(tmp_path, code):
    cfg = p.Config(tmp_path)
    data = cfg.snapshot()
    data["api_key"] = "PRIVATE-KEY"
    cfg.save(data)
    response = NS(status_code=code, headers={"Retry-After": "120"}, json=lambda: {"ok": True})
    session = NS(request=Mock(return_value=response))
    api = p.API(cfg, session)
    if code in {200, 201}:
        assert api.call("/ping") == {"ok": True}
    else:
        with pytest.raises(p.APIError) as error:
            api.call("/orders", {"idempotency_key": "same-key"})
        assert "PRIVATE" not in str(error.value)
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["allow_redirects"] is False
    if code == 429:
        assert api.cooldown > time.monotonic() + 100
        with pytest.raises(p.APIError):
            api.call("/ping")
        assert session.request.call_count == 1


def test_network_exception_does_not_leak_credentials(tmp_path):
    cfg = p.Config(tmp_path)
    data = cfg.snapshot()
    data["api_key"] = "PRIVATE-KEY"
    cfg.save(data)
    session = NS(request=Mock(side_effect=requests.Timeout("PRIVATE-KEY")))
    with pytest.raises(p.APIError) as exc:
        p.API(cfg, session).call("/orders", {"idempotency_key": "same-key"})
    assert "PRIVATE-KEY" not in str(exc.value)


def message(text="/bulk", user=1, chat_type="private"):
    return NS(id=4, text=text, chat=NS(id=1, type=chat_type), from_user=NS(id=user))


def click(e, action, user=1, token=None):
    session = e.panel.sessions[(1, 1)]
    call = NS(message=NS(id=session["mid"], chat=NS(id=1, type="private")), from_user=NS(id=user),
              data=f"bulk:{token or session['token']}:{action}")
    e.panel.callback(call)


def test_telegram_giftcard_wizard(setup):
    s = setup
    panel = s.e.panel
    panel.home(panel.context(message()))
    click(s.e, "lots:0")
    click(s.e, "lot:123")
    click(s.e, "setup:123")
    click(s.e, "kind:giftcards")
    click(s.e, "product:0")
    click(s.e, "offer:0")
    panel.input(message("4.50"))
    panel.input(message("2"))
    click(s.e, "save")
    r = s.e.config.snapshot()["lots"]["123"]
    assert r["params"]["card_id"] == "card" and r["max_cost"] == "4.50" and r["max_units"] == 2
    assert not s.api.orders


@pytest.mark.parametrize("kind,text,expected", [("steam-topup", "100 RUB", {"amount": 100.0, "currency": "RUB"}),
                                                  ("telegram-stars", "100", {"quantity": 100}),
                                                  ("telegram-premium", "6", {"months": 6})])
def test_simple_wizards(setup, kind, text, expected):
    e = setup.e
    e.panel.home(e.panel.context(message()))
    click(e, "setup:123")
    click(e, "kind:" + kind)
    e.panel.input(message(text))
    e.panel.input(message("10"))
    click(e, "save")
    assert e.config.snapshot()["lots"]["123"]["params"] == expected


def test_unauthorized_and_stale_buttons_do_not_spend_or_change_settings(setup):
    e = setup.e
    e.panel.home(e.panel.context(message()))
    before = e.config.snapshot()
    click(e, "toggle", user=2)
    click(e, "toggle", token="stale")
    assert e.config.snapshot() == before


def test_secret_input_is_not_echoed(setup):
    e = setup.e
    e.panel.home(e.panel.context(message()))
    click(e, "key")
    e.panel.input(message("MY-VERY-PRIVATE-KEY"))
    assert e.config.snapshot()["api_key"] == "MY-VERY-PRIVATE-KEY"
    assert not any("MY-VERY-PRIVATE-KEY" in m.text for m in e.c.telegram.bot.messages)
    assert e.c.telegram.bot.deleted == [(1, 4)]


def test_new_funpay_lot_available_without_restart(setup):
    s = setup
    new = copy.copy(s.lots[123])
    new.id, new.description = 456, "Fresh lot"
    s.c.account.get_user = lambda _: NS(id=9, get_sorted_lots=lambda _: {123: s.lots[123], 456: new})
    assert 456 in s.e.refresh_lots() and s.lots[456] is new


def test_serial_worker_handles_simultaneous_duplicate_events(setup):
    e = setup.e
    threads = [threading.Thread(target=lambda: e.submit(e.new_order, setup.event)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    e.pool.submit(lambda: None).result(5)
    assert len(setup.api.orders) == 1
