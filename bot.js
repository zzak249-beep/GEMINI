/**
 * MZ SAMA Bot — ALL BingX symbols + Stats Tracker
 * Scans every USDT perpetual on BingX
 * Simulates TP/SL hits, tracks P&L, sends daily report
 */

require('dotenv').config();

const MultiScanner     = require('./scanner');
const PositionManager  = require('./positions');
const TelegramNotifier = require('./telegram');
const BingXClient      = require('./bingx');
const RiskManager      = require('./risk');
const StatsTracker     = require('./stats');
const logger           = require('./logger');

// ── Config ────────────────────────────────────────────────────────────────────

const CONFIG = {
  bingxApiKey:    process.env.BINGX_API_KEY,
  bingxSecretKey: process.env.BINGX_SECRET_KEY,
  interval:       process.env.INTERVAL        || '1h',

  // SAMA params
  length:         parseInt(process.env.SAMA_LENGTH   || '200'),
  majLength:      parseInt(process.env.MAJ_LENGTH    || '14'),
  minLength:      parseInt(process.env.MIN_LENGTH    || '6'),
  slopePeriod:    parseInt(process.env.SLOPE_PERIOD  || '34'),
  slopeInRange:   parseInt(process.env.SLOPE_RANGE   || '25'),
  flat:           parseInt(process.env.FLAT          || '17'),

  // Scanner — ALL symbols above min volume
  maxSymbols:     parseInt(process.env.MAX_SYMBOLS   || '300'), // up to 300 pairs
  minVolume24h:   parseFloat(process.env.MIN_VOL     || '500000'), // $500k min

  // Position management
  maxPositions:   parseInt(process.env.MAX_POSITIONS || '5'),
  leverage:       parseInt(process.env.LEVERAGE      || '5'),
  riskPct:        parseFloat(process.env.RISK_PCT    || '1'),
  tpPct:          parseFloat(process.env.TP_PCT      || '2'),
  slPct:          parseFloat(process.env.SL_PCT      || '1'),
  minQty:         parseFloat(process.env.MIN_QTY     || '0.001'),
  qtyStep:        parseFloat(process.env.QTY_STEP    || '0.001'),

  // Reporting
  summaryEvery:   parseInt(process.env.SUMMARY_EVERY || '6'),   // market overview every N scans
  dailyReportHour: parseInt(process.env.REPORT_HOUR  || '8'),   // UTC hour for daily report
  dryRun:         process.env.DRY_RUN !== 'false',
};

CONFIG.warmupCandles = CONFIG.length + 60;

// ── Clients ───────────────────────────────────────────────────────────────────

const bingx     = new BingXClient(CONFIG.bingxApiKey, CONFIG.bingxSecretKey);
const telegram  = new TelegramNotifier(process.env.TELEGRAM_TOKEN, process.env.TELEGRAM_CHAT_ID);
const scanner   = new MultiScanner(CONFIG);
const positions = new PositionManager(CONFIG);
const risk      = new RiskManager(CONFIG);
const stats     = new StatsTracker();

let scanCount    = 0;
let lastReportDay = null;

// ── Helpers ───────────────────────────────────────────────────────────────────

function intervalToMs(iv) {
  const map = { '1m':60e3,'3m':180e3,'5m':300e3,'15m':900e3,'30m':1800e3,
                '1h':3600e3,'2h':7200e3,'4h':14400e3,'6h':21600e3,'1d':86400e3 };
  return map[iv] || 3600e3;
}

async function getBalance() {
  if (CONFIG.dryRun) return 1000;
  const res  = await bingx.getBalance();
  const usdt = (res.data?.balance || []).find(b => b.asset === 'USDT');
  return parseFloat(usdt?.availableMargin || usdt?.balance || '0');
}

// ── Daily report scheduler ────────────────────────────────────────────────────

async function maybeSendDailyReport() {
  const now  = new Date();
  const hour = now.getUTCHours();
  const day  = now.toISOString().slice(0, 10);

  if (hour === CONFIG.dailyReportHour && day !== lastReportDay) {
    lastReportDay = day;
    if (stats.stats.totalTrades > 0) {
      await telegram.send(stats.buildDailyReport());
      logger.info('Daily report sent');
    }
  }
}

// ── Trade execution ───────────────────────────────────────────────────────────

async function openLong(r) {
  const balance    = await getBalance();
  const perTrade   = balance / CONFIG.maxPositions;
  const qty        = risk.calcQuantity(perTrade, r.price);
  const { tp, sl } = risk.calcTPSL(r.price, 'LONG');

  if (!CONFIG.dryRun) {
    await bingx.setLeverage(r.symbol, CONFIG.leverage, 'LONG');
    await bingx.marketOrder(r.symbol, 'BUY', 'LONG', qty);
    await bingx.setTPSL(r.symbol, 'LONG', tp, sl);
  }

  positions.open(r.symbol, { side: 'LONG', entry: r.price, qty, tp, sl });
  stats.openTrade(r.symbol, { side: 'LONG', entry: r.price, qty, leverage: CONFIG.leverage, tp, sl, sama: r.sama, slope: r.slope });

  await telegram.sendBuy({ symbol: r.symbol, price: r.price, quantity: qty, leverage: CONFIG.leverage, tp, sl, sama: r.sama, slope: r.slope });
}

async function openShort(r) {
  const balance    = await getBalance();
  const perTrade   = balance / CONFIG.maxPositions;
  const qty        = risk.calcQuantity(perTrade, r.price);
  const { tp, sl } = risk.calcTPSL(r.price, 'SHORT');

  if (!CONFIG.dryRun) {
    await bingx.setLeverage(r.symbol, CONFIG.leverage, 'SHORT');
    await bingx.marketOrder(r.symbol, 'SELL', 'SHORT', qty);
    await bingx.setTPSL(r.symbol, 'SHORT', tp, sl);
  }

  positions.open(r.symbol, { side: 'SHORT', entry: r.price, qty, tp, sl });
  stats.openTrade(r.symbol, { side: 'SHORT', entry: r.price, qty, leverage: CONFIG.leverage, tp, sl, sama: r.sama, slope: r.slope });

  await telegram.sendSell({ symbol: r.symbol, price: r.price, quantity: qty, leverage: CONFIG.leverage, tp, sl, sama: r.sama, slope: r.slope });
}

async function closePosition(symbol, exitPrice, reason = 'signal') {
  const pos = positions.get(symbol);
  if (!pos) return;

  if (!CONFIG.dryRun) {
    await bingx.cancelAllOrders(symbol);
    await bingx.closePosition(symbol, pos.side);
  }

  const record = stats.closeTrade(symbol, exitPrice, reason);
  positions.close(symbol);

  if (record) {
    await telegram.send(stats.buildClosedTradeMsg(record));
  }
}

// ── Main scan loop ────────────────────────────────────────────────────────────

async function tick() {
  scanCount++;
  logger.info(`━━━ Scan #${scanCount} ━━━  Positions: ${positions.openCount}/${CONFIG.maxPositions}`);

  try {
    // 1. Scan ALL symbols
    const allResults = await scanner.scanAll(CONFIG.warmupCandles);
    if (allResults.length === 0) {
      logger.warn('No results from scanner');
      return;
    }

    // 2. Check TP/SL hits on open positions (simulate in DryRun)
    const tpslClosed = stats.checkAllTPSL(allResults);
    for (const record of tpslClosed) {
      positions.close(record.symbol);
      await telegram.send(stats.buildClosedTradeMsg(record));
      logger.info(`TP/SL hit: ${record.symbol} ${record.reason} PnL=${record.netPnl.toFixed(2)}`);
    }

    // 3. Detect new signals
    const newSignals = scanner.getNewSignals(allResults);

    // 4. Handle reverse signals on existing positions
    for (const pos of positions.all) {
      const r = allResults.find(x => x.symbol === pos.symbol);
      if (!r) continue;
      if (pos.side === 'LONG'  && r.signal === 'SELL') await closePosition(pos.symbol, r.price, 'reverse');
      if (pos.side === 'SHORT' && r.signal === 'BUY')  await closePosition(pos.symbol, r.price, 'reverse');
    }

    // 5. Notify all new signals
    if (newSignals.length > 0) {
      logger.info(`New signals: ${newSignals.length} — ${newSignals.map(s=>`${s.symbol}:${s.signal}`).join(', ')}`);
      await telegram.sendMultiSignal(newSignals);
    }

    // 6. Open positions (best slope first, within capacity)
    const tradeable = newSignals
      .filter(s => !positions.has(s.symbol))
      .sort((a, b) => Math.abs(b.slope) - Math.abs(a.slope)); // strongest trend first

    for (const sig of tradeable) {
      if (!positions.hasCapacity) break;
      try {
        if (sig.signal === 'BUY')  await openLong(sig);
        if (sig.signal === 'SELL') await openShort(sig);
        await new Promise(r => setTimeout(r, 250));
      } catch (err) {
        logger.error(`Open position error ${sig.symbol}: ${err.message}`);
        await telegram.sendError(`open ${sig.symbol}`, err.message);
      }
    }

    // 7. Periodic market overview
    if (scanCount % CONFIG.summaryEvery === 0) {
      const overview = scanner.getMarketOverview(allResults);
      const topBull  = allResults.filter(r => r.color === 'bull').sort((a,b) => b.slope - a.slope);
      const topBear  = allResults.filter(r => r.color === 'bear').sort((a,b) => a.slope - b.slope);
      const balance  = await getBalance();

      await telegram.sendScanSummary({ ...overview, interval: CONFIG.interval, topBull, topBear });
      await telegram.sendPositionsSummary(positions.all, balance.toFixed(2));

      // Mini stats update
      if (stats.stats.totalTrades > 0) {
        await telegram.send(
`📈 <b>Stats rápidas</b>
Trades: <b>${stats.stats.totalTrades}</b>  WR: <b>${stats.winRate}%</b>
PnL: <code>${stats.stats.totalPnl >= 0 ? '+' : ''}${stats.stats.totalPnl.toFixed(2)} USDT</code>
Profit Factor: <b>${stats.profitFactor}</b>  MaxDD: <code>-${stats.stats.maxDrawdown.toFixed(2)}</code>`
        );
      }
    }

    // 8. Daily full report at configured UTC hour
    await maybeSendDailyReport();

    logger.info(`Scan #${scanCount} done. Signals: ${newSignals.length}  Results: ${allResults.length}  Pairs scanned: ${allResults.length}`);

  } catch (err) {
    logger.error(`Tick #${scanCount} error: ${err.message}`);
    await telegram.sendError(`tick #${scanCount}`, err.message);
  }
}

// ── Entry ─────────────────────────────────────────────────────────────────────

async function main() {
  logger.info('╔══════════════════════════════════════════╗');
  logger.info('║  MZ SAMA — ALL Symbols + Stats Tracker  ║');
  logger.info('╚══════════════════════════════════════════╝');
  logger.info(`Interval: ${CONFIG.interval} | MaxSymbols: ${CONFIG.maxSymbols} | MinVol: $${CONFIG.minVolume24h.toLocaleString()} | DryRun: ${CONFIG.dryRun}`);

  if (!CONFIG.bingxApiKey || !CONFIG.bingxSecretKey) {
    logger.error('Missing BINGX_API_KEY / BINGX_SECRET_KEY');
    process.exit(1);
  }

  await telegram.sendStart(CONFIG);

  // If resuming with existing stats
  if (stats.stats.totalTrades > 0) {
    logger.info(`Resuming: ${stats.stats.totalTrades} trades tracked, PnL=${stats.stats.totalPnl.toFixed(2)}`);
    await telegram.send(`♻️ <b>Bot reiniciado</b> — retomando ${stats.stats.totalTrades} trades históricos\nPnL acumulado: <code>${stats.stats.totalPnl.toFixed(2)} USDT</code>`);
  }

  await tick();
  setInterval(tick, intervalToMs(CONFIG.interval));
}

main().catch(async err => {
  logger.error('Fatal: ' + err.message);
  await telegram.sendError('main()', err.message);
  process.exit(1);
});
