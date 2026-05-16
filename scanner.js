/**
 * Full-Market Scanner — TODAS las monedas de BingX
 * Sin límite de símbolos. Filtra solo por volumen mínimo.
 * Procesa en batches para respetar rate limits.
 */

const BingXClient  = require('./bingx');
const SAMAStrategy = require('./strategy');
const logger       = require('./logger');

class MultiScanner {
  constructor(config) {
    this.config      = config;
    this.bingx       = new BingXClient(config.bingxApiKey, config.bingxSecretKey);
    this.strategies  = new Map();   // symbol → SAMAStrategy
    this.lastSignals = new Map();   // symbol → last color/signal

    // Solo filtra volumen mínimo — SIN límite de cantidad de símbolos
    this.minVolume24h = config.minVolume24h || 100_000;  // $100k mínimo por defecto
    this.batchSize    = config.batchSize    || 8;        // peticiones paralelas por batch
    this.batchDelayMs = config.batchDelayMs || 400;      // ms entre batches
  }

  // ── 1. Obtener TODOS los pares USDT-perpetual activos ────────────────────

  async getAllSymbols() {
    try {
      const res = await this.bingx._get('/openApi/swap/v2/quote/contracts');
      const contracts = res.data || [];

      const symbols = contracts
        .filter(c => c.symbol.endsWith('-USDT') && c.status === 1)
        .map(c => c.symbol);

      logger.info(`BingX contratos activos USDT: ${symbols.length}`);
      return symbols;
    } catch (err) {
      logger.error(`getAllSymbols: ${err.message}`);
      return [];
    }
  }

  // ── 2. Obtener tickers 24h y filtrar por volumen ─────────────────────────

  async getFilteredSymbols() {
    try {
      // Intenta endpoint de todos los tickers a la vez
      const res = await this.bingx._get('/openApi/swap/v2/quote/ticker');
      const tickers = Array.isArray(res.data) ? res.data : [];

      const filtered = tickers
        .filter(t => {
          if (!t.symbol.endsWith('-USDT')) return false;
          const vol = parseFloat(t.quoteVolume || t.volume || 0);
          return vol >= this.minVolume24h;
        })
        .sort((a, b) =>
          parseFloat(b.quoteVolume || b.volume || 0) -
          parseFloat(a.quoteVolume || a.volume || 0)
        )
        .map(t => ({
          symbol: t.symbol,
          price:  parseFloat(t.lastPrice  || 0),
          vol24h: parseFloat(t.quoteVolume || t.volume || 0),
          change: parseFloat(t.priceChangePercent || 0)
        }));

      logger.info(`Pares con vol ≥ $${this.minVolume24h.toLocaleString()}: ${filtered.length}`);
      return filtered;

    } catch (err) {
      // Fallback: usa solo la lista de contratos sin filtro de volumen
      logger.warn(`getFilteredSymbols ticker error: ${err.message} — usando contratos sin filtro`);
      const symbols = await this.getAllSymbols();
      return symbols.map(s => ({ symbol: s, price: 0, vol24h: 0, change: 0 }));
    }
  }

  // ── 3. Calcular SAMA para un símbolo ─────────────────────────────────────

  async scanSymbol(symbol, warmupCandles) {
    try {
      const res = await this.bingx.getKlines(symbol, this.config.interval, warmupCandles + 5);
      if (!res.data || res.data.length < 60) return null;

      const candles = res.data.map(k => ({
        time:  k[0],
        open:  parseFloat(k[1]),
        high:  parseFloat(k[2]),
        low:   parseFloat(k[3]),
        close: parseFloat(k[4]),
        vol:   parseFloat(k[5])
      }));

      // Reutiliza instancia de estrategia por símbolo
      if (!this.strategies.has(symbol)) {
        this.strategies.set(symbol, new SAMAStrategy(this.config));
      }
      const strat = this.strategies.get(symbol);
      strat.reset();

      let result = null;
      for (const candle of candles) {
        result = strat.update(candle);
      }

      if (!result || result.sama === null) return null;

      const last = candles[candles.length - 2]; // última vela cerrada
      return {
        symbol,
        price:  last.close,
        high:   last.high,
        low:    last.low,
        vol:    last.vol,
        ...result
      };
    } catch (err) {
      // Par sin datos suficientes, deslistado, etc. — silencioso
      return null;
    }
  }

  // ── 4. Escanear TODOS los símbolos en batches ─────────────────────────────

  async scanAll(warmupCandles) {
    const symbolList = await this.getFilteredSymbols();
    if (symbolList.length === 0) return [];

    logger.info(`Iniciando scan de ${symbolList.length} pares...`);

    const results  = [];
    const total    = symbolList.length;
    let   done     = 0;

    for (let i = 0; i < total; i += this.batchSize) {
      const batch = symbolList.slice(i, i + this.batchSize);

      const batchResults = await Promise.all(
        batch.map(t => this.scanSymbol(t.symbol, warmupCandles))
      );

      const valid = batchResults.filter(Boolean);
      results.push(...valid);
      done += batch.length;

      // Log progreso cada 50 pares
      if (done % 50 === 0 || done === total) {
        logger.info(`  Progreso: ${done}/${total} pares — ${results.length} con datos`);
      }

      // Pausa entre batches para no saturar la API
      if (i + this.batchSize < total) {
        await new Promise(r => setTimeout(r, this.batchDelayMs));
      }
    }

    logger.info(`Scan completo: ${results.length}/${total} pares procesados`);
    return results;
  }

  // ── 5. Detectar señales NUEVAS (solo cambios de color) ───────────────────

  getNewSignals(scanResults) {
    const newSignals = [];

    for (const r of scanResults) {
      const prev   = this.lastSignals.get(r.symbol);
      const isNew  =
        (r.signal === 'BUY'  && prev !== 'BUY')  ||
        (r.signal === 'SELL' && prev !== 'SELL');

      if (isNew) {
        newSignals.push(r);
        this.lastSignals.set(r.symbol, r.signal);
      } else {
        this.lastSignals.set(r.symbol, r.color);
      }
    }

    return newSignals;
  }

  // ── 6. Resumen del mercado ────────────────────────────────────────────────

  getMarketOverview(scanResults) {
    const bull  = scanResults.filter(r => r.color === 'bull').length;
    const bear  = scanResults.filter(r => r.color === 'bear').length;
    const chop  = scanResults.filter(r => r.color === 'chop').length;
    const buys  = scanResults.filter(r => r.signal === 'BUY').length;
    const sells = scanResults.filter(r => r.signal === 'SELL').length;
    const total = scanResults.length;
    return { bull, bear, chop, buys, sells, total };
  }
}

module.exports = MultiScanner;
