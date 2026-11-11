"""Broker adapters. Both expose the same small interface so the agent doesn't care which one runs.

PaperBroker  : virtual account, real gold prices from Yahoo (GC=F). Used until Exness demo is connected.
MT5Broker    : Exness MT5 directly (terminal installed on the GitHub Windows runner each run).
MetaApiBroker: Exness MT5 through MetaApi cloud (optional alternative).
"""
import asyncio
import os

import pandas as pd

from . import config, memory


def _yahoo_candles(interval="1h", period="60d"):
    import yfinance as yf

    df = yf.download(config.PAPER_DATA_TICKER, interval=interval, period=period,
                     progress=False, auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df


# --------------------------------------------------------------------------- paper
class PaperBroker:
    name = "paper"

    def __init__(self, state):
        self.state = state
        self._df = None

    def candles(self):
        if self._df is None:
            self._df = _yahoo_candles()
        return self._df

    def price(self):
        c = float(self.candles()["close"].iloc[-1])
        h = config.PAPER_SPREAD / 2
        return {"bid": c - h, "ask": c + h, "time": self.candles().index[-1]}

    def account(self):
        bal = self.state["paper_balance"]
        p = self.price()
        floating = sum(self._pnl(pos, p["bid"] if pos["side"] == "BUY" else p["ask"]) for pos in self.state["open"])
        return {"balance": bal, "equity": bal + floating}

    def positions(self):
        return list(self.state["open"])

    @staticmethod
    def _pnl(pos, exit_price):
        d = exit_price - pos["entry"] if pos["side"] == "BUY" else pos["entry"] - exit_price
        return d * pos["lots"] * config.CONTRACT_SIZE

    def open(self, side, lots, sl, tp, comment=""):
        p = self.price()
        pos = {
            "id": f"P{self.state['next_id']}", "side": side, "lots": lots, "sl": sl, "tp": tp,
            "entry": p["ask"] if side == "BUY" else p["bid"], "initial_sl": sl,
            "open_time": memory.iso(), "checked_until": str(p["time"]),
        }
        self.state["next_id"] += 1
        self.state["open"].append(pos)
        return pos

    def modify_sl(self, pos_id, sl):
        for pos in self.state["open"]:
            if pos["id"] == pos_id:
                pos["sl"] = sl

    def sync_closed(self):
        """Walk candles since the last check and close positions whose SL/TP got hit."""
        df, closed, still = self.candles(), [], []
        for pos in self.state["open"]:
            bars = df[df.index > pd.Timestamp(pos["checked_until"])]
            hit = None
            for t, b in bars.iterrows():
                if pos["side"] == "BUY":
                    if b.low <= pos["sl"]:
                        hit = (pos["sl"], "SL", t)
                    elif b.high >= pos["tp"]:
                        hit = (pos["tp"], "TP", t)
                else:
                    if b.high + config.PAPER_SPREAD >= pos["sl"]:
                        hit = (pos["sl"], "SL", t)
                    elif b.low + config.PAPER_SPREAD <= pos["tp"]:
                        hit = (pos["tp"], "TP", t)
                if hit:
                    break  # if SL and TP are in the same bar we assume SL (conservative)
            if hit:
                pnl = self._pnl(pos, hit[0])
                self.state["paper_balance"] += pnl
                closed.append({**pos, "exit": hit[0], "exit_reason": hit[1], "pnl": round(pnl, 2)})
            else:
                if len(bars):
                    pos["checked_until"] = str(bars.index[-1])
                still.append(pos)
        self.state["open"] = still
        return closed

    def close(self):
        pass


# --------------------------------------------------------------------------- metaapi
class MetaApiBroker:
    name = "metaapi"

    def __init__(self, state):
        from metaapi_cloud_sdk import MetaApi

        self.state = state
        self.loop = asyncio.new_event_loop()
        self.api = MetaApi(os.environ["METAAPI_TOKEN"])
        self.account_id = os.environ["METAAPI_ACCOUNT_ID"]
        self.acc = self._r(self.api.metatrader_account_api.get_account(self.account_id))
        if self.acc.state != "DEPLOYED":
            self._r(self.acc.deploy())
        self._r(self.acc.wait_connected())
        self.conn = self.acc.get_rpc_connection()
        self._r(self.conn.connect())
        self._r(self.conn.wait_synchronized())
        self.spec = self._r(self.conn.get_symbol_specification(config.SYMBOL))
        self._df = None
        state.setdefault("live_open", {})

    def _r(self, coro):
        return self.loop.run_until_complete(coro)

    def candles(self):
        if self._df is None:
            try:
                raw = self._r(self.acc.get_historical_candles(config.SYMBOL, "1h", None, 1000))
                df = pd.DataFrame(raw)
                df.index = pd.to_datetime(df["time"], utc=True)
                self._df = df[["open", "high", "low", "close"]].astype(float).sort_index()
            except Exception as e:  # fall back to Yahoo if history API is unavailable
                print("MetaApi candles failed, using Yahoo:", e)
                self._df = _yahoo_candles()
        return self._df

    def price(self):
        p = self._r(self.conn.get_symbol_price(config.SYMBOL))
        return {"bid": p["bid"], "ask": p["ask"], "time": pd.Timestamp(p["time"])}

    def account(self):
        a = self._r(self.conn.get_account_information())
        return {"balance": a["balance"], "equity": a["equity"]}

    def positions(self):
        out = []
        for p in self._r(self.conn.get_positions()):
            if p.get("symbol") != config.SYMBOL:
                continue
            known = self.state["live_open"].get(str(p["id"]), {})
            out.append({
                "id": str(p["id"]), "side": "BUY" if p["type"].endswith("BUY") else "SELL",
                "lots": p["volume"], "entry": p["openPrice"], "sl": p.get("stopLoss"),
                "tp": p.get("takeProfit"), "initial_sl": known.get("initial_sl", p.get("stopLoss")),
                "open_time": str(p.get("time")),
            })
        return out

    def lot_limits(self):
        return (self.spec.get("minVolume", 0.01), self.spec.get("maxVolume", 100),
                self.spec.get("volumeStep", 0.01), self.spec.get("contractSize", config.CONTRACT_SIZE))

    def open(self, side, lots, sl, tp, comment="ai-agent"):
        fn = self.conn.create_market_buy_order if side == "BUY" else self.conn.create_market_sell_order
        res = self._r(fn(config.SYMBOL, lots, sl, tp, {"comment": comment[:30]}))
        pid = str(res.get("positionId") or res.get("orderId"))
        pr = self.price()
        pos = {"id": pid, "side": side, "lots": lots, "sl": sl, "tp": tp, "initial_sl": sl,
               "entry": pr["ask"] if side == "BUY" else pr["bid"], "open_time": memory.iso()}
        self.state["live_open"][pid] = pos
        return pos

    def modify_sl(self, pos_id, sl):
        pos = next((p for p in self.positions() if p["id"] == pos_id), None)
        if pos:
            self._r(self.conn.modify_position(pos_id, sl, pos["tp"]))

    def sync_closed(self):
        live_ids = {p["id"] for p in self.positions()}
        closed = []
        for pid, pos in list(self.state["live_open"].items()):
            if pid in live_ids:
                continue
            deals = self._r(self.conn.get_deals_by_position(pid))
            deals = deals.get("deals", deals) if isinstance(deals, dict) else deals
            pnl = sum(d.get("profit", 0) + d.get("commission", 0) + d.get("swap", 0) for d in deals)
            out = [d for d in deals if d.get("entryType") == "DEAL_ENTRY_OUT"]
            exit_price = out[-1]["price"] if out else pos["entry"]
            reason = "TP" if pos["tp"] and abs(exit_price - pos["tp"]) < abs(exit_price - pos["sl"]) else "SL"
            closed.append({**pos, "exit": exit_price, "exit_reason": reason, "pnl": round(pnl, 2)})
            del self.state["live_open"][pid]
        return closed

    def close(self):
        try:
            self._r(self.conn.close())
        except Exception:
            pass

# --------------------------------------------------------------------------- mt5 (direct)
class MT5Broker:
    """Exness MT5 directly: the terminal is installed on the GitHub Windows runner each run,
    and the official MetaTrader5 Python package logs in and trades. No third-party service."""
    name = "mt5"
    MAGIC = 420042

    def __init__(self, state):
        import glob
        import time

        import MetaTrader5 as mt5

        self.mt5, self.state = mt5, state
        path = os.getenv("MT5_PATH") or next(iter(glob.glob(r"C:\Program Files\*\terminal64.exe")), None)
        login, pwd, server = int(os.environ["EXNESS_LOGIN"]), os.environ["EXNESS_PASSWORD"], os.environ["EXNESS_SERVER"]
        ok = False
        for attempt in range(4):
            # attach to the (already running) terminal first, then log in separately
            # attempts 0,2: attach to the running terminal; 1,3: let the package start it
            kw = {} if attempt % 2 == 0 else {"path": path, "portable": True}
            if mt5.initialize(timeout=90_000, **kw):
                ok = mt5.login(login, password=pwd, server=server, timeout=90_000)
                if ok:
                    break
                print("MT5 attached but login failed:", mt5.last_error())
            else:
                print("MT5 init failed, retrying:", mt5.last_error())
            mt5.shutdown()
            time.sleep(10)
        if not ok:
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        info = mt5.account_info()
        print(f"MT5 connected: login {info.login} on {info.server}, balance {info.balance} {info.currency}")
        self.symbol = self._resolve_symbol(config.SYMBOL)
        config.SYMBOL = self.symbol
        self.info = mt5.symbol_info(self.symbol)
        self._df = None
        state.setdefault("live_open", {})

    def _resolve_symbol(self, want):
        mt5 = self.mt5
        if mt5.symbol_info(want) is None:
            cands = [s.name for s in (mt5.symbols_get("*XAUUSD*") or [])]
            if not cands:
                raise RuntimeError("Gold symbol not found on this account")
            want = sorted(cands, key=len)[0]  # e.g. XAUUSDm on Exness Standard
        mt5.symbol_select(want, True)
        return want

    def candles(self):
        if self._df is None:
            rates = self.mt5.copy_rates_from_pos(self.symbol, self.mt5.TIMEFRAME_H1, 0, 1000)
            if rates is None or len(rates) == 0:
                print("MT5 candles failed, using Yahoo:", self.mt5.last_error())
                self._df = _yahoo_candles()
            else:
                df = pd.DataFrame(rates)
                df.index = pd.to_datetime(df["time"], unit="s", utc=True)
                self._df = df[["open", "high", "low", "close"]].astype(float).sort_index()
        return self._df

    def price(self):
        t = self.mt5.symbol_info_tick(self.symbol)
        return {"bid": t.bid, "ask": t.ask, "time": pd.Timestamp(t.time, unit="s", tz="UTC")}

    def account(self):
        a = self.mt5.account_info()
        return {"balance": a.balance, "equity": a.equity}

    def positions(self):
        out = []
        for p in self.mt5.positions_get(symbol=self.symbol) or []:
            known = self.state["live_open"].get(str(p.ticket), {})
            out.append({
                "id": str(p.ticket), "side": "BUY" if p.type == self.mt5.POSITION_TYPE_BUY else "SELL",
                "lots": p.volume, "entry": p.price_open, "sl": p.sl or None, "tp": p.tp or None,
                "initial_sl": known.get("initial_sl", p.sl), "open_time": str(pd.Timestamp(p.time, unit="s")),
            })
        return out

    def lot_limits(self):
        i = self.info
        return (i.volume_min, i.volume_max, i.volume_step, i.trade_contract_size)

    def _send(self, req):
        mt5 = self.mt5
        res = None
        for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
            res = mt5.order_send({**req, "type_filling": filling})
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                return res
            if res is not None and res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                break
        raise RuntimeError(f"Order failed: {res.retcode if res else mt5.last_error()} {res.comment if res else ''}")

    def open(self, side, lots, sl, tp, comment="ai-agent"):
        mt5 = self.mt5
        t = mt5.symbol_info_tick(self.symbol)
        d = self.info.digits
        res = self._send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": t.ask if side == "BUY" else t.bid, "sl": round(sl, d), "tp": round(tp, d),
            "deviation": 30, "magic": self.MAGIC, "comment": comment[:25],
            "type_time": mt5.ORDER_TIME_GTC,
        })
        pid = str(res.order)
        pos = {"id": pid, "side": side, "lots": lots, "sl": sl, "tp": tp, "initial_sl": sl,
               "entry": res.price or (t.ask if side == "BUY" else t.bid), "open_time": memory.iso()}
        self.state["live_open"][pid] = pos
        return pos

    def modify_sl(self, pos_id, sl):
        pos = next((p for p in self.positions() if p["id"] == pos_id), None)
        if pos:
            d = self.info.digits
            self._send({"action": self.mt5.TRADE_ACTION_SLTP, "symbol": self.symbol,
                        "position": int(pos_id), "sl": round(sl, d), "tp": pos["tp"] or 0.0})

    def sync_closed(self):
        from datetime import datetime, timedelta, timezone

        live_ids = {p["id"] for p in self.positions()}
        closed = []
        for pid, pos in list(self.state["live_open"].items()):
            if pid in live_ids:
                continue
            deals = self.mt5.history_deals_get(position=int(pid)) or []
            if not deals:
                now = datetime.now(timezone.utc)
                deals = [x for x in (self.mt5.history_deals_get(now - timedelta(days=30), now + timedelta(days=1)) or [])
                         if x.position_id == int(pid)]
            pnl = sum(x.profit + x.commission + x.swap + getattr(x, "fee", 0) for x in deals)
            outs = [x for x in deals if x.entry == self.mt5.DEAL_ENTRY_OUT]
            exit_price = outs[-1].price if outs else pos["entry"]
            reason = "TP" if pos["tp"] and abs(exit_price - pos["tp"]) < abs(exit_price - pos["sl"]) else "SL"
            closed.append({**pos, "exit": exit_price, "exit_reason": reason, "pnl": round(pnl, 2)})
            del self.state["live_open"][pid]
        return closed

    def close(self):
        self.mt5.shutdown()


def get_broker(state):
    if config.MODE == "mt5":
        return MT5Broker(state)
    if config.MODE == "metaapi":
        return MetaApiBroker(state)
    return PaperBroker(state)
