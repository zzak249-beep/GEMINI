/**
 * Stats Tracker — DryRun P&L simulator
 * Tracks every signal across ALL BingX pairs
 * Sends daily Telegram report with full statistics
 */

const fs     = require('fs');
const path   = require('path');

const STATS_FILE = path.join(__dirname, '..', 'logs', 'stats.json');

class StatsTracker {
  constructor() {
    this.stats = this._load();
  }

  // ── Persistence ──────────────────────────────────────────────────────────

  _load() {
    try {
      if (fs.existsSync(STATS_FILE)) {
        return JSON.parse(fs.readFileSync(STATS_FILE, 'utf8'));
      }
    } catch (_) {}
    return this._empty();
  }

  _empty() {
    return {
      startedAt:   new Date().toISOString(),
      totalTrades: 0,
      wins:        0,
      losses:      0,
      totalPnl:    0,
      totalFees:   0,
      maxWin:      0,
      maxLoss:     0,
      maxDrawdown: 0,
      peakPnl:     0,
      trades:      [],           // last 200 trades stored
      daily:       {},           // date → { trades, pnl, wins, losses }
      bySymbol:    {},           // symbol → { trades, pnl, wins, losses }
      openTrades:  {}            // symbol → { side, entry, tp, sl, qty, openedAt, sama, slope }
    };
  }

  _save() {
    try {
      const dir = path.dirname(STATS_FILE);
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
      // Keep only last 200 trades to avoid file bloat
      if (this.stats.trades.length > 200) {
        this.stats.trades = this.stats.trades.slice(-200);
      }
      fs.writeFileSync(STATS_FILE, JSON.stringify(this.stats, null, 2));
    } catch (err) {
      console.error('[Stats] Save error:', err.message);
    }
  }

  // ── Trade lifecycle ──────────────────────────────────────────────────────

  openTrade(symbol, { side, entry, tp, sl, qty, leverage, sama, slope }) {
    this.stats.openTrades[symbol] = {
      symbol, side, entry, tp, sl, qty, leverage, sama, slope,
      openedAt: new Date().toISOString()
    };
    this._save();
  }

  closeTrade(symbol, exitPrice, reason = 'signal') {
    const trade = this.stats.openTrades[symbol];
    if (!trade) return null;

    const { side, entry, qty, leverage, openedAt, sama, slope } = trade;

    // Raw P&L (before fees)
    const rawPnl = side === 'LONG'
      ? (exitPrice - entry) * qty * leverage
      : (entry - exitPrice) * qty * leverage;

    // Simulate BingX taker fees: 0.075% × 2 sides × notional
    const notional = entry * qty;
    const fees     = notional * 0.00075 * 2;
    const netPnl   = rawPnl - fees;

    const durationMs  = Date.now() - new Date(openedAt).getTime();
    const durationMin = Math.round(durationMs / 60000);
    const today       = new Date().toISOString().slice(0, 10);
    const isWin       = netPnl > 0;

    // ── Update aggregate stats ──
    this.stats.totalTrades++;
    this.stats.totalPnl  += netPnl;
    this.stats.totalFees += fees;
    if (isWin) { this.stats.wins++;   this.stats.maxWin  = Math.max(this.stats.maxWin,  netPnl); }
    else       { this.stats.losses++; this.stats.maxLoss = Math.min(this.stats.maxLoss, netPnl); }

    // Drawdown tracking
    this.stats.peakPnl     = Math.max(this.stats.peakPnl, this.stats.totalPnl);
    const dd               = this.stats.peakPnl - this.stats.totalPnl;
    this.stats.maxDrawdown = Math.max(this.stats.maxDrawdown, dd);

    // ── Daily ──
    if (!this.stats.daily[today]) {
      this.stats.daily[today] = { trades: 0, pnl: 0, wins: 0, losses: 0, fees: 0 };
    }
    this.stats.daily[today].trades++;
    this.stats.daily[today].pnl    += netPnl;
    this.stats.daily[today].fees   += fees;
    if (isWin) this.stats.daily[today].wins++;
    else       this.stats.daily[today].losses++;

    // ── By symbol ──
    if (!this.stats.bySymbol[symbol]) {
      this.stats.bySymbol[symbol] = { trades: 0, pnl: 0, wins: 0, losses: 0 };
    }
    this.stats.bySymbol[symbol].trades++;
    this.stats.bySymbol[symbol].pnl += netPnl;
    if (isWin) this.stats.bySymbol[symbol].wins++;
    else       this.stats.bySymbol[symbol].losses++;

    // ── Trade log ──
    const record = {
      symbol, side, entry, exit: exitPrice,
      qty, leverage, rawPnl, fees, netPnl,
      reason, durationMin, sama, slope, isWin,
      closedAt: new Date().toISOString()
    };
    this.stats.trades.push(record);

    delete this.stats.openTrades[symbol];
    this._save();

    return record;
  }

  // ── Simulated TP/SL hit check ────────────────────────────────────────────

  checkTPSL(symbol, currentPrice) {
    const trade = this.stats.openTrades[symbol];
    if (!trade) return null;

    const { side, tp, sl } = trade;

    if (side === 'LONG') {
      if (currentPrice >= tp) return this.closeTrade(symbol, tp, 'TP');
      if (currentPrice <= sl) return this.closeTrade(symbol, sl, 'SL');
    } else {
      if (currentPrice <= tp) return this.closeTrade(symbol, tp, 'TP');
      if (currentPrice >= sl) return this.closeTrade(symbol, sl, 'SL');
    }
    return null;
  }

  checkAllTPSL(scanResults) {
    const closed = [];
    for (const r of scanResults) {
      const result = this.checkTPSL(r.symbol, r.price);
      if (result) closed.push(result);
    }
    return closed;
  }

  // ── Report generation ────────────────────────────────────────────────────

  get winRate() {
    return this.stats.totalTrades > 0
      ? (this.stats.wins / this.stats.totalTrades * 100).toFixed(1)
      : '0.0';
  }

  get avgPnl() {
    return this.stats.totalTrades > 0
      ? (this.stats.totalPnl / this.stats.totalTrades).toFixed(2)
      : '0.00';
  }

  get profitFactor() {
    const grossWin  = this.stats.trades.filter(t => t.netPnl > 0).reduce((s,t) => s + t.netPnl, 0);
    const grossLoss = Math.abs(this.stats.trades.filter(t => t.netPnl < 0).reduce((s,t) => s + t.netPnl, 0));
    return grossLoss > 0 ? (grossWin / grossLoss).toFixed(2) : '∞';
  }

  // Top/bottom symbols by P&L
  get topSymbols() {
    return Object.entries(this.stats.bySymbol)
      .map(([sym, s]) => ({ sym, ...s }))
      .sort((a, b) => b.pnl - a.pnl);
  }

  // Last 7 days daily summary
  get last7Days() {
    const days = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      const key = d.toISOString().slice(0, 10);
      days.push({ date: key, ...(this.stats.daily[key] || { trades: 0, pnl: 0, wins: 0, losses: 0, fees: 0 }) });
    }
    return days;
  }

  buildDailyReport() {
    const s        = this.stats;
    const today    = new Date().toISOString().slice(0, 10);
    const todayD   = s.daily[today] || { trades: 0, pnl: 0, wins: 0, losses: 0, fees: 0 };
    const top5     = this.topSymbols.slice(0, 5);
    const bot5     = this.topSymbols.slice(-5).reverse();
    const days     = this.last7Days;
    const openCnt  = Object.keys(s.openTrades).length;

    // Daily chart bar (last 7 days)
    const dayChart = days.map(d => {
      const bar = d.pnl >= 0 ? '🟩' : '🟥';
      return `${bar} ${d.date.slice(5)} ${d.pnl >= 0 ? '+' : ''}${d.pnl.toFixed(1)}`;
    }).join('\n');

    const topList = top5.length
      ? top5.map(t => `  🏆 <code>${t.sym.padEnd(14)}</code> +${t.pnl.toFixed(2)} (${t.wins}W/${t.losses}L)`).join('\n')
      : '  —';

    const botList = bot5.length
      ? bot5.map(t => `  💀 <code>${t.sym.padEnd(14)}</code> ${t.pnl.toFixed(2)} (${t.wins}W/${t.losses}L)`).join('\n')
      : '  —';

    const startDate = s.startedAt.slice(0, 10);
    const pnlEmoji  = s.totalPnl >= 0 ? '📈' : '📉';
    const todayEmoji = todayD.pnl >= 0 ? '✅' : '❌';

    return `📊 <b>DAILY REPORT — MZ SAMA</b>
━━━━━━━━━━━━━━━━━━━━
${pnlEmoji} <b>Total PnL:</b> <code>${s.totalPnl >= 0 ? '+' : ''}${s.totalPnl.toFixed(2)} USDT</code>
${todayEmoji} <b>Hoy:</b> <code>${todayD.pnl >= 0 ? '+' : ''}${todayD.pnl.toFixed(2)} USDT</code>  (${todayD.trades} trades)
💸 <b>Fees pagadas:</b> <code>${s.totalFees.toFixed(2)} USDT</code>
━━━━━━━━━━━━━━━━━━━━
📋 <b>Estadísticas globales</b>
  Trades: <b>${s.totalTrades}</b>  (${s.wins}W / ${s.losses}L)
  Win rate: <b>${this.winRate}%</b>
  Avg PnL/trade: <code>${this.avgPnl} USDT</code>
  Profit Factor: <b>${this.profitFactor}</b>
  Max Win: <code>+${s.maxWin.toFixed(2)}</code>  Max Loss: <code>${s.maxLoss.toFixed(2)}</code>
  Max Drawdown: <code>-${s.maxDrawdown.toFixed(2)} USDT</code>
  Posiciones abiertas: <b>${openCnt}</b>
  Desde: ${startDate}
━━━━━━━━━━━━━━━━━━━━
📅 <b>Últimos 7 días</b>
${dayChart}
━━━━━━━━━━━━━━━━━━━━
<b>Top 5 mejores pares:</b>
${topList}
<b>Top 5 peores pares:</b>
${botList}
⏰ ${new Date().toUTCString()}`;
  }

  buildClosedTradeMsg(record) {
    const e = record.isWin ? '✅' : '❌';
    const dur = record.durationMin < 60
      ? `${record.durationMin}m`
      : `${(record.durationMin/60).toFixed(1)}h`;
    return `${e} <b>${record.symbol}</b> ${record.side} [${record.reason}]
💰 <code>${record.netPnl >= 0 ? '+' : ''}${record.netPnl.toFixed(2)} USDT</code>  ⏱ ${dur}
📍 ${record.entry} → ${record.exit}  fees: ${record.fees.toFixed(3)}`;
  }

  reset() {
    this.stats = this._empty();
    this._save();
  }
}

module.exports = StatsTracker;
