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

    def candles_tf(self, tf):
        try:
            return _yahoo_candles(tf, "60d" if tf == "15m" else "2y")
        except Exception as e:
            print(f"Yahoo {tf} candles unavailable:", e)
            return None

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

    def candles_tf(self, tf):
        try:
            raw = self._r(self.acc.get_historical_candles(config.SYMBOL, tf, None, 600 if tf == "15m" else 300))
            df = pd.DataFrame(raw)
            df.index = pd.to_datetime(df["time"], utc=True)
            return df[["open", "high", "low", "close"]].astype(float).sort_index()
        except Exception as e:
            print(f"MetaApi {tf} candles unavailable:", e)
            return None

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
    """Exness MT5 directly: the terminal is installed and logged in on the GitHub Windows runner,
    and our AgentBridge EA inside it trades. Python talks to the EA through text files in
    MQL5/Files (the MetaTrader5 Python IPC does not work on the hosted runner)."""
    name = "mt5"

    def __init__(self, state):
        import time

        self.state = state
        term = os.getenv("MT5_PATH") or r"C:\Program Files\MetaTrader 5\terminal64.exe"
        self.dir = os.path.join(os.path.dirname(term), "MQL5", "Files")
        self.st = {}
        for _ in range(90):
            self.st = self._read_state()
            if self.st.get("symbol") and float(self.st.get("bid", 0)) > 0 and self.st.get("connected") == "1":
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"AgentBridge EA not ready, last state: {self.st}")
        self.symbol = config.SYMBOL = self.st["symbol"]
        print(f"MT5 connected: login {self.st['login']} on {self.st['server']}, balance {self.st['balance']} "
              f"{self.st['currency']}, symbol {self.symbol}, trade_allowed={self.st.get('trade_allowed')}")
        self._df = None
        state.setdefault("live_open", {})

    def _read_state(self):
        st, pos = {}, []
        try:
            with open(os.path.join(self.dir, "state.txt"), encoding="latin-1") as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    if k == "pos":
                        pos.append(v.split(";"))
                    elif k:
                        st[k] = v
        except OSError:
            return {}
        st["_pos"] = pos
        return st

    def _cmd(self, *parts, timeout=60):
        import time
        import uuid

        cid = uuid.uuid4().hex[:8]
        res_path = os.path.join(self.dir, "res.txt")
        if os.path.exists(res_path):
            os.remove(res_path)
        tmp = os.path.join(self.dir, "cmd.tmp")
        with open(tmp, "w", encoding="latin-1") as f:
            f.write("|".join([cid, *map(str, parts)]))
        os.replace(tmp, os.path.join(self.dir, "cmd.txt"))
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.5)
            try:
                with open(res_path, encoding="latin-1") as f:
                    out = f.read().strip().split("|")
            except OSError:
                continue
            if out and out[0] == cid:
                if out[1] != "ok":
                    raise RuntimeError(f"MT5 {parts[0]} failed: {'|'.join(out[2:])}")
                return out[2:]
        raise RuntimeError(f"MT5 {parts[0]}: no answer from AgentBridge EA")

    def candles(self):
        if self._df is None:
            try:
                df = pd.read_csv(os.path.join(self.dir, "rates.csv"))
                df.index = pd.to_datetime(df["time"], unit="s", utc=True)
                self._df = df[["open", "high", "low", "close"]].astype(float).sort_index()
                if len(self._df) < 200:
                    raise ValueError("too few bars")
            except Exception as e:
                print("MT5 candles failed, using Yahoo:", e)
                self._df = _yahoo_candles()
        return self._df

    def candles_tf(self, tf):
        """'15m' or '1d' straight from the EA (exact broker prices). None if not available."""
        name = {"15m": "rates_m15.csv", "1d": "rates_d1.csv"}.get(tf)
        try:
            df = pd.read_csv(os.path.join(self.dir, name))
            df.index = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df[["open", "high", "low", "close"]].astype(float).sort_index()
            return df if len(df) >= 50 else None
        except Exception as e:
            print(f"MT5 {tf} candles unavailable:", e)
            return None

    def price(self):
        st = self._read_state() or self.st
        return {"bid": float(st["bid"]), "ask": float(st["ask"]), "time": pd.Timestamp(int(st["time"]), unit="s", tz="UTC")}

    def account(self):
        st = self._read_state() or self.st
        return {"balance": float(st["balance"]), "equity": float(st["equity"])}

    def positions(self):
        st = self._read_state() or self.st
        out = []
        for tk, side, vol, entry, sl, tp, t in st.get("_pos", []):
            known = self.state["live_open"].get(tk, {})
            sl, tp = float(sl) or None, float(tp) or None
            out.append({"id": tk, "side": side, "lots": float(vol), "entry": float(entry), "sl": sl, "tp": tp,
                        "initial_sl": known.get("initial_sl", sl), "open_time": str(pd.Timestamp(int(t), unit="s"))})
        return out

    def lot_limits(self):
        st = self.st
        return (float(st["vmin"]), float(st["vmax"]), float(st["vstep"]), float(st["csize"]))

    def open(self, side, lots, sl, tp, comment="ai-agent"):
        d = int(self.st["digits"])
        ticket, price = self._cmd("open", side, f"{lots:.2f}", round(sl, d), round(tp, d), comment[:25].replace("|", " "))
        pos = {"id": ticket, "side": side, "lots": lots, "sl": sl, "tp": tp, "initial_sl": sl,
               "entry": float(price) or self.price()["ask" if side == "BUY" else "bid"], "open_time": memory.iso()}
        self.state["live_open"][ticket] = pos
        return pos

    def modify_sl(self, pos_id, sl):
        pos = next((p for p in self.positions() if p["id"] == pos_id), None)
        if pos:
            d = int(self.st["digits"])
            self._cmd("modify", pos_id, round(sl, d), round(pos["tp"] or 0.0, d))

    def sync_closed(self):
        live_ids = {p["id"] for p in self.positions()}
        closed = []
        for pid, pos in list(self.state["live_open"].items()):
            if pid in live_ids:
                continue
            deals = [x.split(";") for x in self._cmd("deals", pid)]
            pnl = sum(float(x[2]) + float(x[3]) + float(x[4]) + float(x[5]) for x in deals)
            outs = [x for x in deals if x[0] in ("1", "2")]  # DEAL_ENTRY_OUT / INOUT
            exit_price = float(outs[-1][1]) if outs else pos["entry"]
            reason = "TP" if pos["tp"] and abs(exit_price - pos["tp"]) < abs(exit_price - pos["sl"]) else "SL"
            closed.append({**pos, "exit": exit_price, "exit_reason": reason, "pnl": round(pnl, 2)})
            del self.state["live_open"][pid]
        return closed

    def close_position(self, pos_id):
        return self._cmd("close", pos_id)

    def close(self):
        pass


def get_broker(state):
    if config.MODE == "mt5":
        return MT5Broker(state)
    if config.MODE == "metaapi":
        return MetaApiBroker(state)
    return PaperBroker(state)
