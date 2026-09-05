"""
Cliente para BingX Perpetual Swap (v2) — repo joyful-art.

Firma HMAC-SHA256: el query string se construye UNA sola vez, ordenado,
y se usa exactamente igual para firmar y para enviar (evita el bug clásico
de firmar en un orden y transmitir en otro).

CORRECCIONES respecto a la versión anterior:
  - En modo HEDGE, BingX rechaza con 109400 cualquier orden que lleve el
    campo reduceOnly, TAMBIÉN al cerrar. El comentario anterior decía que
    solo fallaba al abrir; era falso, y por eso close_position() no
    funcionaba nunca: ni el cierre normal ni el de emergencia ni el
    /emergency-stop. En hedge no hace falta el campo: side + positionSide
    ya determinan que se reduce.
  - Se cierra por el positionAmt REAL leído de BingX, no por la cantidad
    calculada al abrir (difieren tras fills parciales o SL/TP parciales).
  - get_symbol_filters filtra por símbolo: /quote/contracts puede ignorar
    el parámetro y devolver la lista entera, y coger el primero a ciegas
    daba la precisión de OTRO contrato.
  - open_protected_position(): abre y no deja la posición sin stop.
"""
import hashlib
import hmac
import logging
import time

import requests

import config

log = logging.getLogger("bingx")

TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.6

# La cuenta opera en modo HEDGE (positionSide=LONG/SHORT explícito).
# Se puede forzar One-Way con HEDGE_MODE=false en el entorno.
HEDGE_MODE = bool(getattr(config, "HEDGE_MODE", True))

ERR_HEDGE_FIELD_NOT_ALLOWED = 109400
ERR_POSITION_NOT_EXIST = 109420


class BingXError(Exception):
    pass


class BingXClient:
    def __init__(self, api_key=None, api_secret=None, base_url=None):
        # .strip() defensivo: una key/secret con un '\n' o espacio colado
        # (típico al pegar variables en Railway) rompe la cabecera HTTP
        # X-BX-APIKEY con un ValueError críptico en pleno reconcile/entrada.
        self.api_key = (api_key or config.BINGX_API_KEY or "").strip()
        self.api_secret = (api_secret or config.BINGX_API_SECRET or "").strip()

        # BingX usa una URL DISTINTA para demo (VST) que para producción.
        # Si el caller no pasa base_url, se resuelve según BINGX_DEMO --
        # así BINGX_DEMO=true apunta de verdad a dinero simulado y no es
        # solo una etiqueta sin efecto.
        if base_url:
            self.base_url = base_url.strip()
        elif config.BINGX_DEMO:
            self.base_url = "https://open-api-vst.bingx.com"
        else:
            self.base_url = (config.BINGX_BASE_URL or "https://open-api.bingx.com").strip()

        self._filters_cache = {}  # symbol -> (fetched_at, filters_dict)
        if not self.api_key or not self.api_secret:
            log.warning("BINGX_API_KEY / BINGX_API_SECRET no configuradas.")
        log.info(
            "BingXClient inicializado contra %s (%s) | modo posición: %s",
            self.base_url,
            "DEMO/VST" if config.BINGX_DEMO else "PRODUCCIÓN REAL",
            "HEDGE" if HEDGE_MODE else "ONE-WAY",
        )

    # ------------------------------------------------------------------ #
    def _signed_request(self, method: str, path: str, params: dict):
        params = {k: v for k, v in params.items() if v is not None}
        params["recvWindow"] = params.get("recvWindow", "10000")
        timestamp = str(int(time.time() * 1000))

        # BingX firma así (parseParam de su SDK oficial): todos los
        # parámetros ordenados alfabéticamente, concatenados EN CRUDO como
        # "clave=valor" (nada de urlencode/percent-encoding) y con
        # "timestamp" añadido el último, fuera del sort. Dos bugs igual de
        # importantes, ya corregidos:
        #   1. timestamp dentro del sorted().
        #   2. urlencode() sobre el query string -- rompe la firma en
        #      cualquier orden con stopLoss/takeProfit, porque ese valor es
        #      JSON embebido lleno de caracteres que urlencode transforma
        #      (%7B, %22...) y que BingX firma en su forma literal.
        ordered_items = sorted(params.items())
        query_string = "&".join(f"{k}={v}" for k, v in ordered_items)
        query_string = f"{query_string}&timestamp={timestamp}" if query_string else f"timestamp={timestamp}"

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        headers = {"X-BX-APIKEY": self.api_key}

        # Reintentos solo para fallos de red/timeout, NUNCA para rechazos
        # de la API (esos son definitivos: fondos insuficientes, símbolo
        # inválido... reintentarlos no cambia el resultado y puede duplicar
        # efectos secundarios).
        last_network_exc = None
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(method, url, headers=headers, timeout=TIMEOUT)
                data = resp.json()
                last_network_exc = None
                break
            except Exception as e:
                last_network_exc = e
                if attempt < MAX_RETRIES:
                    log.warning(
                        "Fallo de red llamando a %s (intento %d/%d): %s — reintentando",
                        path, attempt, MAX_RETRIES, e,
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        if last_network_exc is not None:
            raise BingXError(f"Fallo de red/parseo llamando a {path}: {last_network_exc}") from last_network_exc

        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error en {path}: {data}")
        return data.get("data", data)

    # ------------------------------------------------------------------ #
    def get_balance(self) -> float:
        """Devuelve el equity disponible en USDT de la cuenta de swap."""
        data = self._signed_request("GET", "/openApi/swap/v2/user/balance", {})
        balances = data.get("balance", data)
        if isinstance(balances, dict):
            return float(balances.get("equity", balances.get("balance", 0)))
        if isinstance(balances, list):
            for b in balances:
                if b.get("asset") == "USDT":
                    return float(b.get("equity", b.get("balance", 0)))
        return 0.0

    def get_positions(self, symbol: str = None):
        params = {"symbol": symbol} if symbol else {}
        data = self._signed_request("GET", "/openApi/swap/v2/user/positions", params)
        return data if isinstance(data, list) else data.get("positions", [])

    def get_position_amt(self, symbol: str, position_side: str) -> float:
        """Tamaño REAL abierto en BingX para ese símbolo y lado (absoluto).
        0.0 si no hay nada. Cerrar SIEMPRE con esto: la cantidad calculada
        al abrir difiere tras un fill parcial o un SL/TP que ya redujo
        parte, y una cantidad mayor que la real hace que BingX rechace la
        orden o, peor, abra posición en sentido contrario."""
        wanted = (position_side or "").upper()
        try:
            positions = self.get_positions(symbol)
        except Exception:
            log.exception("No se pudo leer la posición real de %s", symbol)
            return 0.0
        for p in positions:
            if str(p.get("symbol")) != symbol:
                continue
            side = str(p.get("positionSide", "")).upper()
            if HEDGE_MODE and side and side != wanted:
                continue
            amt = float(p.get("positionAmt", 0) or 0)
            if amt:
                return abs(amt)
        return 0.0

    def set_leverage(self, symbol: str, side: str, leverage: int):
        """side: 'LONG' o 'SHORT' (modo hedge, que es el que usa este bot)."""
        return self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/leverage",
            {"symbol": symbol, "side": side, "leverage": leverage},
        )

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED"):
        return self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            {"symbol": symbol, "marginType": mode},
        )

    def place_market_order(
        self,
        symbol: str,
        side: str,           # "BUY" / "SELL"
        position_side: str,  # "LONG" / "SHORT"
        quantity: float,
        stop_loss: float = None,
        take_profit: float = None,
        reduce_only: bool = False,
    ):
        params = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": quantity,
        }
        # En modo HEDGE, BingX rechaza con 109400 ("In the Hedge mode, the
        # 'ReduceOnly' field can not be filled") CUALQUIER orden que lleve
        # el campo reduceOnly -- al abrir Y al cerrar, con valor "true" o
        # "false", solo por estar presente. Y no hace falta: en hedge,
        # side + positionSide ya determinan unívocamente que se reduce
        # (SELL + positionSide=LONG solo puede cerrar el largo, nunca abre
        # un corto). El campo se manda solo en One-Way, donde sí evita que
        # la posición se dé la vuelta.
        if reduce_only and not HEDGE_MODE:
            params["reduceOnly"] = "true"
        if stop_loss:
            params["stopLoss"] = (
                '{"type":"STOP_MARKET","stopPrice":%s,"workingType":"MARK_PRICE"}'
                % stop_loss
            )
        if take_profit:
            params["takeProfit"] = (
                '{"type":"TAKE_PROFIT_MARKET","stopPrice":%s,"workingType":"MARK_PRICE"}'
                % take_profit
            )
        return self._signed_request("POST", "/openApi/swap/v2/trade/order", params)

    def place_stop_market(self, symbol: str, side: str, position_side: str,
                          stop_price: float, quantity: float):
        """SL como orden condicional independiente, con stopPrice numérico.

        Es el plan B cuando el SL embebido en la orden de entrada no se
        confirma. Se manda con quantity y NUNCA con closePosition, que en
        hedge puede rechazarse igual que reduceOnly."""
        return self._signed_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "STOP_MARKET",
            "stopPrice": self.round_price(symbol, stop_price),
            "quantity": self.round_qty(symbol, quantity),
        })

    def place_take_profit_market(self, symbol: str, side: str, position_side: str,
                                 stop_price: float, quantity: float):
        return self._signed_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": self.round_price(symbol, stop_price),
            "quantity": self.round_qty(symbol, quantity),
        })

    def get_open_orders(self, symbol: str):
        """Órdenes abiertas (incluye las condicionales de SL/TP) para un
        símbolo. Se usa justo después de abrir una posición para confirmar
        que el SL/TP realmente se adjuntó -- si BingX lo rechaza en
        silencio, la posición queda desprotegida y nunca se cierra sola."""
        data = self._signed_request(
            "GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol}
        )
        orders = data if isinstance(data, list) else data.get("orders", [])
        return orders

    def has_stop_and_take_profit(self, symbol: str) -> bool:
        """True si hay al menos una orden STOP_MARKET/STOP y una
        TAKE_PROFIT_MARKET/TAKE_PROFIT abiertas para el símbolo."""
        try:
            orders = self.get_open_orders(symbol)
        except Exception:
            log.exception("No se pudo verificar SL/TP de %s tras abrir la orden", symbol)
            return False
        types = {o.get("type", "").upper() for o in orders}
        has_sl = any("STOP" in t and "TAKE" not in t for t in types)
        has_tp = any("TAKE" in t for t in types)
        return has_sl and has_tp

    def protection_status(self, symbol: str, position_side: str = None):
        """(hay_sl, hay_tp) por lado. Más fino que has_stop_and_take_profit:
        permite dejar abierta una posición que tiene SL pero no TP en vez de
        cerrarla, porque con el stop puesto el riesgo ya está acotado."""
        wanted = (position_side or "").upper()
        try:
            orders = self.get_open_orders(symbol)
        except Exception:
            log.exception("No se pudo verificar SL/TP de %s", symbol)
            return (False, False)
        has_sl = has_tp = False
        for o in orders:
            side = str(o.get("positionSide", "")).upper()
            if wanted and side and side != wanted:
                continue
            otype = str(o.get("type", "")).upper()
            if "TAKE" in otype:
                has_tp = True
            elif "STOP" in otype:
                has_sl = True
        return (has_sl, has_tp)

    def cancel_all_open_orders(self, symbol: str):
        """Cancela las órdenes abiertas del símbolo (condicionales de SL/TP
        incluidas). Tras cerrar una posición hay que llamarla: si no, las
        condicionales huérfanas siguen vivas y al dispararse más tarde
        pueden ABRIR una posición nueva no deseada."""
        return self._signed_request(
            "DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol}
        )

    def close_position(self, symbol: str, position_side: str, quantity: float = None):
        """Cierra a mercado con la orden contraria.

        quantity=None (recomendado) lee el tamaño REAL de la posición.
        Devuelve None si no había nada que cerrar, para que el caller no
        confunda "no había posición" con "el cierre falló".
        """
        position_side = (position_side or "").upper()
        if quantity is None:
            quantity = self.get_position_amt(symbol, position_side)
        quantity = abs(float(quantity or 0))
        if quantity <= 0:
            log.info("%s (%s): no hay posición abierta que cerrar", symbol, position_side)
            return None

        quantity = self.round_qty(symbol, quantity)
        side = "SELL" if position_side == "LONG" else "BUY"
        return self.place_market_order(
            symbol, side, position_side, quantity, reduce_only=True
        )

    def close_position_and_verify(self, symbol: str, position_side: str,
                                  retries: int = 2) -> bool:
        """Cierra y COMPRUEBA contra BingX que quedó a cero, cancelando
        después las condicionales huérfanas. Un 'ok' de la API no garantiza
        el cierre: la orden puede quedar parcialmente ejecutada."""
        position_side = (position_side or "").upper()
        for attempt in range(1, retries + 2):
            try:
                self.close_position(symbol, position_side)
            except Exception:
                log.exception("%s (%s): fallo enviando el cierre (intento %d)",
                              symbol, position_side, attempt)
            time.sleep(1.0)
            if self.get_position_amt(symbol, position_side) <= 0:
                try:
                    self.cancel_all_open_orders(symbol)
                except Exception:
                    log.warning("%s: cerrada, pero quedaron condicionales sin cancelar", symbol)
                log.info("%s (%s): cerrada y verificada", symbol, position_side)
                return True
            log.warning("%s (%s): sigue abierta tras el cierre — reintentando",
                        symbol, position_side)
        log.error("%s (%s): NO se pudo cerrar tras %d intentos — REVISAR A MANO",
                  symbol, position_side, retries + 1)
        return False

    # ------------------------------------------------------------------ #
    def open_protected_position(self, symbol: str, position_side: str, quantity: float,
                                stop_loss: float, take_profit: float,
                                leverage: int = None, margin_mode: str = "ISOLATED") -> dict:
        """Abre y NO deja la posición sin protección.

        Intenta primero con el SL/TP embebido en la orden de entrada (es
        atómico: no hay ventana en la que la posición exista sin stop). Si
        BingX no los confirma, los coloca como condicionales separadas. Si
        tras eso sigue sin haber SL, CIERRA la posición: una entrada sin
        stop en una cuenta cruzada es peor que no haber entrado.

        Devuelve {"ok", "quantity", "has_sl", "has_tp", "closed", "error"}.
        """
        position_side = (position_side or "").upper()
        if position_side not in ("LONG", "SHORT"):
            raise ValueError(f"position_side inválido: {position_side}")
        entry_side = "BUY" if position_side == "LONG" else "SELL"
        exit_side = "SELL" if position_side == "LONG" else "BUY"
        result = {"ok": False, "quantity": 0.0, "has_sl": False,
                  "has_tp": False, "closed": False, "error": None}

        # Coherencia dirección/precios: un SL al otro lado de la entrada se
        # dispara al instante y cierra nada más abrir.
        if position_side == "LONG" and not (stop_loss < take_profit):
            result["error"] = f"LONG: SL {stop_loss} debe ser < TP {take_profit}"
            return result
        if position_side == "SHORT" and not (stop_loss > take_profit):
            result["error"] = f"SHORT: SL {stop_loss} debe ser > TP {take_profit}"
            return result

        # Margen aislado: con CROSS, una pérdida grande aquí se come margen
        # de otras posiciones y de todo el equity, no solo lo previsto por
        # el sizing. Falla en silencio porque BingX devuelve error si ya
        # estaba en ISOLATED, y eso no debe bloquear una entrada válida.
        try:
            self.set_margin_mode(symbol, margin_mode)
        except Exception:
            log.warning("%s: no se pudo confirmar margin mode %s", symbol, margin_mode)

        if leverage:
            try:
                self.set_leverage(symbol, position_side, int(leverage))
            except Exception as exc:
                log.warning("%s: no se pudo fijar apalancamiento %s (%s)", symbol, leverage, exc)

        qty = self.round_qty(symbol, quantity)
        if qty <= 0:
            result["error"] = "cantidad redondeada a 0"
            return result

        try:
            self.place_market_order(symbol, entry_side, position_side, qty,
                                    stop_loss=stop_loss, take_profit=take_profit)
        except Exception as exc:
            result["error"] = f"entrada rechazada: {exc}"
            log.error("%s (%s): %s", symbol, position_side, result["error"])
            return result

        # Tamaño REAL rellenado: proteger por la cantidad pedida dejaría
        # parte sin cubrir si el fill fue parcial.
        time.sleep(0.5)
        filled = self.get_position_amt(symbol, position_side)
        if filled <= 0:
            result["error"] = "la entrada no aparece como posición abierta"
            log.error("%s (%s): %s", symbol, position_side, result["error"])
            return result
        result["quantity"] = filled

        has_sl, has_tp = self.protection_status(symbol, position_side)

        # Plan B: el SL/TP embebido no se confirmó -> condicionales sueltas.
        if not has_sl:
            try:
                self.place_stop_market(symbol, exit_side, position_side, stop_loss, filled)
            except Exception as exc:
                log.warning("%s: fallo colocando el SL como orden separada: %s", symbol, exc)
        if not has_tp:
            try:
                self.place_take_profit_market(symbol, exit_side, position_side, take_profit, filled)
            except Exception as exc:
                log.warning("%s: fallo colocando el TP como orden separada: %s", symbol, exc)

        has_sl, has_tp = self.protection_status(symbol, position_side)
        result["has_sl"], result["has_tp"] = has_sl, has_tp

        if not has_sl:
            log.error("%s (%s): SIN STOP tras abrir — cerrando la posición",
                      symbol, position_side)
            result["closed"] = self.close_position_and_verify(symbol, position_side)
            result["error"] = "no se pudo colocar el SL; posición cerrada"
            return result

        if not has_tp:
            # Con SL puesto el riesgo está acotado: se deja abierta y se
            # avisa, en vez de cerrar una operación válida por el TP.
            log.warning("%s (%s): abierta con SL pero SIN TP", symbol, position_side)

        result["ok"] = True
        return result

    # ------------------------------------------------------------------ #
    def get_symbol_filters(self, symbol: str):
        """Precisión de cantidad/precio del símbolo (evita rechazos por
        decimales).

        Se filtra por símbolo sobre la respuesta: este endpoint puede
        devolver la lista completa de contratos aunque se le pase symbol, y
        coger items[0] a ciegas daría la precisión de OTRO contrato -- con
        lo que la cantidad se redondea mal y BingX rechaza la orden.
        """
        data = self._signed_request(
            "GET", "/openApi/swap/v2/quote/contracts", {"symbol": symbol}
        )
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return item
        if len(items) == 1 and isinstance(items[0], dict) and items[0].get("symbol"):
            return items[0]
        log.warning("No se encontró el contrato %s en la respuesta de BingX", symbol)
        return {}

    def get_symbol_filters_cached(self, symbol: str, ttl_seconds: int = 3600):
        """Igual que get_symbol_filters pero cacheado en memoria (evita una
        llamada extra en cada entrada; la precisión casi nunca cambia)."""
        now = time.time()
        cached = self._filters_cache.get(symbol)
        if cached and (now - cached[0]) < ttl_seconds:
            return cached[1]
        filters = self.get_symbol_filters(symbol)
        self._filters_cache[symbol] = (now, filters)
        return filters

    def round_qty(self, symbol: str, qty: float) -> float:
        """Ajusta qty a la precisión/tamaño mínimo que exige BingX. Si no
        consigue leer los filtros, redondea a 3 decimales como fallback en
        vez de reventar la entrada."""
        try:
            filters = self.get_symbol_filters_cached(symbol)
            precision = int(filters.get("quantityPrecision", 3))
            min_qty = float(filters.get("tradeMinQuantity", filters.get("minQty", 0)) or 0)
        except Exception:
            log.warning("No se pudo leer precisión de %s, uso fallback de 3 decimales", symbol)
            precision, min_qty = 3, 0.0

        rounded = round(float(qty), precision)
        if min_qty and 0 < rounded < min_qty:
            rounded = min_qty
        return rounded

    def round_qty_up(self, symbol: str, qty: float) -> float:
        """Como round_qty pero SIEMPRE hacia arriba (math.ceil a la
        precisión del contrato).

        Necesario para el suelo de nocional: round_qty redondea al más
        cercano, así que subir la cantidad al mínimo y luego redondear
        podía dejarla otra vez por debajo del mínimo. Aquí se garantiza
        que el resultado nunca baja del valor pedido.
        """
        import math
        try:
            filters = self.get_symbol_filters_cached(symbol)
            precision = int(filters.get("quantityPrecision", 3))
            min_qty = float(filters.get("tradeMinQuantity", filters.get("minQty", 0)) or 0)
        except Exception:
            precision, min_qty = 3, 0.0
        if precision < 0:
            precision = 0
        factor = 10 ** precision
        subida = math.ceil(float(qty) * factor) / factor
        if min_qty and 0 < subida < min_qty:
            subida = min_qty
        return subida

    def round_price(self, symbol: str, price: float) -> float:
        """Redondea el precio a la precisión del contrato. Sin esto, en
        tokens de precio bajo un SL puede acabar en 0.0 o igualado a la
        entrada -- el mismo bug que apareció en fibstruct con 1000CHEEMS."""
        try:
            filters = self.get_symbol_filters_cached(symbol)
            precision = int(filters.get("pricePrecision", 6))
        except Exception:
            precision = 6
        return round(float(price), precision)

    # ------------------------------------------------------------------ #
    # Mercado público (sin firma) — velas para calcular la señal nosotros
    # mismos en vez de depender de TradingView.
    # ------------------------------------------------------------------ #
    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 200):
        """Devuelve velas OHLCV crudas de BingX (no requiere API key)."""
        url = f"{self.base_url}/openApi/swap/v3/quote/klines"
        try:
            resp = requests.get(
                url,
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=TIMEOUT,
            )
            data = resp.json()
        except Exception as e:
            raise BingXError(f"Fallo obteniendo klines de {symbol}: {e}") from e
        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error en klines de {symbol}: {data}")
        return data.get("data", [])

    def get_all_symbols(self, quote_filter: str = "USDT"):
        """Lista todos los perpetuos disponibles en BingX (endpoint público).
        quote_filter=None para traerlos todos.

        Aplica EXCLUDE_PREFIXES si está definido en config: antes esa
        variable existía en Railway pero no la leía nadie en este repo, así
        que símbolos como NCCOGOLD2USD-USDT seguían entrando en el universo
        pese a estar excluidos.
        """
        url = f"{self.base_url}/openApi/swap/v2/quote/contracts"
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            data = resp.json()
        except Exception as e:
            raise BingXError(f"Fallo listando símbolos de BingX: {e}") from e
        if data.get("code") not in (0, None):
            raise BingXError(f"BingX API error listando símbolos: {data}")

        prefijos = tuple(getattr(config, "EXCLUDE_PREFIXES", ()) or ())
        rows = data.get("data", [])
        symbols = []
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            status = r.get("status", r.get("apiStateOpen", 1))
            if status in (0, False, "OFFLINE"):
                continue
            if quote_filter and not sym.endswith(f"-{quote_filter}"):
                continue
            base = sym.split("-")[0].upper()
            if prefijos and base.startswith(prefijos):
                continue
            symbols.append(sym)
        return sorted(set(symbols))

    # ------------------------------------------------------------------ #
    # PnL realizado — para saber cuánto se ganó/perdió cuando una posición
    # se cierra sola por el SL/TP (BingX la cierra él mismo; este bot se
    # entera por reconciliación, no por una orden propia).
    # ------------------------------------------------------------------ #
    def get_income(self, symbol: str, income_type: str = "REALIZED_PNL",
                   start_time: int = None, limit: int = 100):
        params = {"symbol": symbol, "incomeType": income_type, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        data = self._signed_request("GET", "/openApi/swap/v2/user/income", params)
        return data if isinstance(data, list) else data.get("income", [])

    def get_realized_pnl_since(self, symbol: str, start_time_ms: int = None) -> float:
        rows = self.get_income(symbol, "REALIZED_PNL", start_time_ms)
        return sum(float(r.get("income", 0)) for r in rows)

    # ------------------------------------------------------------------ #
    # Liquidez — para filtrar símbolos ilíquidos ANTES de operarlos, no
    # después de comerse el spread.
    # ------------------------------------------------------------------ #
    def get_all_tickers(self):
        """Estadísticas de 24h de todos los símbolos (precio, volumen...)."""
        data = self._signed_request("GET", "/openApi/swap/v2/quote/ticker", {})
        return data if isinstance(data, list) else data.get("tickers", [])

    def get_24h_quote_volumes(self) -> dict:
        """symbol -> volumen en USDT de las últimas 24h (0 si no se puede
        determinar). Prueba varios nombres de campo porque la documentación
        de BingX no es consistente entre versiones."""
        volumes = {}
        try:
            tickers = self.get_all_tickers()
        except Exception:
            log.exception("No se pudieron leer los tickers de 24h para filtrar liquidez")
            return volumes
        for t in tickers:
            sym = t.get("symbol")
            if not sym:
                continue
            vol = t.get("quoteVolume") or t.get("quoteVol") or t.get("volume") or 0
            try:
                volumes[sym] = float(vol)
            except (TypeError, ValueError):
                volumes[sym] = 0.0
        return volumes
