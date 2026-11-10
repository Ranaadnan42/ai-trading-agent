"""Broker adapters. Both expose the same small interface so the agent doesn't care which one runs.

PaperBroker  : virtual account, real gold prices from Yahoo (GC=F). Used until Exness demo is connected.
MetaApiBroker: real Exness MT5 (demo first) through MetaApi cloud - works from GitHub Actions (no Windows).
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


def get_broker(state):
    if config.MODE == "metaapi":
        return MetaApiBroker(state)
    return PaperBroker(state)
