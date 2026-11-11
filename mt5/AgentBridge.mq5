//+------------------------------------------------------------------+
//| AgentBridge: lets the Python AI agent trade through this terminal |
//| via plain text files in MQL5\Files (no Python IPC needed).        |
//|   state.txt  - written every 0.5s: account, price, positions      |
//|   rates.csv  - H1 candles of the gold symbol                       |
//|   cmd.txt    - one command from Python  -> res.txt (answer)        |
//+------------------------------------------------------------------+
#property strict

input string InpSymbolHint = "XAUUSD";
input long   InpMagic      = 420042;

string g_sym = "";
long   g_seq = 0;
uint   g_rc = 0;
string g_rcs = "";
ulong  g_order = 0;
double g_price = 0;

ENUM_ORDER_TYPE_FILLING Filling()
{
   long m = SymbolInfoInteger(g_sym, SYMBOL_FILLING_MODE);
   if((m & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   if((m & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

bool Send(MqlTradeRequest &rq)
{
   MqlTradeResult rs;
   ZeroMemory(rs);
   rq.magic = InpMagic;
   rq.symbol = g_sym;
   bool ok = OrderSend(rq, rs);
   g_rc = rs.retcode; g_rcs = rs.comment; g_order = rs.order; g_price = rs.price;
   return ok && (rs.retcode == TRADE_RETCODE_DONE || rs.retcode == TRADE_RETCODE_PLACED);
}

string ResolveSymbol(string want)
{
   if(SymbolSelect(want, true) && SymbolInfoInteger(want, SYMBOL_DIGITS) > 0)
      return want;
   string best = "";
   for(int i = 0; i < SymbolsTotal(false); i++)
   {
      string s = SymbolName(i, false);
      if(StringFind(s, want) >= 0 && (best == "" || StringLen(s) < StringLen(best)))
         best = s;
   }
   if(best != "")
      SymbolSelect(best, true);
   return best;
}

void WriteText(string name, string text)
{
   string tmp = name + ".tmp";
   int h = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return;
   FileWriteString(h, text);
   FileClose(h);
   FileMove(tmp, 0, name, FILE_REWRITE);
}

void WriteRates()
{
   MqlRates r[];
   int n = CopyRates(g_sym, PERIOD_H1, 0, 1000, r);
   string out = "time,open,high,low,close\n";
   for(int i = 0; i < n; i++)
      out += StringFormat("%I64d,%.5f,%.5f,%.5f,%.5f\n", (long)r[i].time, r[i].open, r[i].high, r[i].low, r[i].close);
   WriteText("rates.csv", out);
}

void WriteState()
{
   MqlTick t;
   SymbolInfoTick(g_sym, t);
   string out = "";
   out += "seq=" + IntegerToString(++g_seq) + "\n";
   out += "symbol=" + g_sym + "\n";
   out += "login=" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\n";
   out += "server=" + AccountInfoString(ACCOUNT_SERVER) + "\n";
   out += "currency=" + AccountInfoString(ACCOUNT_CURRENCY) + "\n";
   out += "balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\n";
   out += "equity=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\n";
   out += "connected=" + IntegerToString(TerminalInfoInteger(TERMINAL_CONNECTED)) + "\n";
   out += "trade_allowed=" + IntegerToString(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED)) + "\n";
   out += "bid=" + DoubleToString(t.bid, 5) + "\n";
   out += "ask=" + DoubleToString(t.ask, 5) + "\n";
   out += "time=" + IntegerToString((long)t.time) + "\n";
   out += "digits=" + IntegerToString(SymbolInfoInteger(g_sym, SYMBOL_DIGITS)) + "\n";
   out += "vmin=" + DoubleToString(SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MIN), 4) + "\n";
   out += "vmax=" + DoubleToString(SymbolInfoDouble(g_sym, SYMBOL_VOLUME_MAX), 4) + "\n";
   out += "vstep=" + DoubleToString(SymbolInfoDouble(g_sym, SYMBOL_VOLUME_STEP), 4) + "\n";
   out += "csize=" + DoubleToString(SymbolInfoDouble(g_sym, SYMBOL_TRADE_CONTRACT_SIZE), 2) + "\n";
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || PositionGetString(POSITION_SYMBOL) != g_sym) continue;
      out += StringFormat("pos=%I64u;%s;%.4f;%.5f;%.5f;%.5f;%I64d\n", tk,
                          PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL",
                          PositionGetDouble(POSITION_VOLUME), PositionGetDouble(POSITION_PRICE_OPEN),
                          PositionGetDouble(POSITION_SL), PositionGetDouble(POSITION_TP),
                          (long)PositionGetInteger(POSITION_TIME));
   }
   WriteText("state.txt", out);
}

string RunCommand(string line)
{
   string f[];
   int n = StringSplit(line, '|', f);
   if(n < 2) return "?|err|0|bad command";
   string id = f[0], cmd = f[1];
   int d = (int)SymbolInfoInteger(g_sym, SYMBOL_DIGITS);
   if(cmd == "open" && n >= 7)
   {
      double vol = StringToDouble(f[3]), sl = NormalizeDouble(StringToDouble(f[4]), d), tp = NormalizeDouble(StringToDouble(f[5]), d);
      MqlTradeRequest rq;
      ZeroMemory(rq);
      bool buy = f[2] == "BUY";
      rq.action = TRADE_ACTION_DEAL;
      rq.volume = vol;
      rq.type = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      rq.price = SymbolInfoDouble(g_sym, buy ? SYMBOL_ASK : SYMBOL_BID);
      rq.sl = sl; rq.tp = tp; rq.deviation = 30; rq.comment = f[6];
      rq.type_filling = Filling();
      rq.type_time = ORDER_TIME_GTC;
      if(Send(rq))
         return StringFormat("%s|ok|%I64u|%.5f", id, g_order, g_price);
      return StringFormat("%s|err|%u|%s", id, g_rc, g_rcs);
   }
   if(cmd == "modify" && n >= 5)
   {
      ulong tk = (ulong)StringToInteger(f[2]);
      MqlTradeRequest rq;
      ZeroMemory(rq);
      rq.action = TRADE_ACTION_SLTP;
      rq.position = tk;
      rq.sl = NormalizeDouble(StringToDouble(f[3]), d);
      rq.tp = NormalizeDouble(StringToDouble(f[4]), d);
      bool ok = Send(rq);
      return StringFormat("%s|%s|%u|%s", id, ok ? "ok" : "err", g_rc, g_rcs);
   }
   if(cmd == "close" && n >= 3)
   {
      ulong tk = (ulong)StringToInteger(f[2]);
      if(!PositionSelectByTicket(tk)) return id + "|err|0|position not found";
      MqlTradeRequest rq;
      ZeroMemory(rq);
      bool buy = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY;
      rq.action = TRADE_ACTION_DEAL;
      rq.position = tk;
      rq.volume = PositionGetDouble(POSITION_VOLUME);
      rq.type = buy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      rq.price = SymbolInfoDouble(g_sym, buy ? SYMBOL_BID : SYMBOL_ASK);
      rq.deviation = 30;
      rq.type_filling = Filling();
      if(Send(rq))
         return StringFormat("%s|ok|%I64u|%.5f", id, g_order, g_price);
      return StringFormat("%s|err|%u|%s", id, g_rc, g_rcs);
   }
   if(cmd == "deals" && n >= 3)
   {
      string out = id + "|ok";
      if(HistorySelectByPosition((long)StringToInteger(f[2])))
         for(int i = 0; i < HistoryDealsTotal(); i++)
         {
            ulong dt = HistoryDealGetTicket(i);
            out += StringFormat("|%d;%.5f;%.2f;%.2f;%.2f;%.2f", (int)HistoryDealGetInteger(dt, DEAL_ENTRY),
                                HistoryDealGetDouble(dt, DEAL_PRICE), HistoryDealGetDouble(dt, DEAL_PROFIT),
                                HistoryDealGetDouble(dt, DEAL_COMMISSION), HistoryDealGetDouble(dt, DEAL_SWAP),
                                HistoryDealGetDouble(dt, DEAL_FEE));
         }
      return out;
   }
   if(cmd == "rates")
   {
      WriteRates();
      return id + "|ok";
   }
   return id + "|err|0|unknown command";
}

void CheckCommand()
{
   if(!FileIsExist("cmd.txt")) return;
   int h = FileOpen("cmd.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return;
   string line = FileReadString(h);
   FileClose(h);
   FileDelete("cmd.txt");
   StringTrimLeft(line);
   StringTrimRight(line);
   if(line == "") return;
   string res = RunCommand(line);
   Print("AgentBridge: ", line, " -> ", res);
   WriteText("res.txt", res);
}

int OnInit()
{
   Print("AgentBridge init, data path: ", TerminalInfoString(TERMINAL_DATA_PATH), " chart ", _Symbol);
   g_sym = ResolveSymbol(InpSymbolHint);
   if(g_sym == "")
   {
      Print("AgentBridge: gold symbol not found");
      return INIT_FAILED;
   }
   FileDelete("cmd.txt");
   FileDelete("res.txt");
   WriteRates();
   WriteState();
   EventSetMillisecondTimer(500);
   Print("AgentBridge ready on ", g_sym);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
   CheckCommand();
   WriteState();
}

void OnTick() {}
