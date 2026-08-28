"""
main.py
=======
Aplicacao unica: FastAPI + polling em background + cache em memoria
(um dicionario Python simples). Sem banco de dados, sem ORM, sem
camadas de repositorio/service separadas.

Fluxo:
  1. Ao iniciar, dispara uma primeira coleta e agenda um loop
     `asyncio` que repete a coleta a cada POLL_INTERVAL_SECONDS.
  2. Cada coleta busca as instituicoes EM PARALELO (nao uma de cada
     vez) e atualiza o cache PROGRESSIVAMENTE - assim que os dados de
     uma instituicao chegam, ja aparecem no cache, sem esperar todas
     as outras terminarem.
  3. O frontend (static/index.html) faz `fetch()` em /api/dashboard
     a cada alguns segundos (setInterval no JS) e redesenha a tela.

Se qualquer chamada a NDD falhar (timeout, credenciais erradas, etc.),
o erro e logado e guardado em cache["error"], mas o servidor CONTINUA
rodando com os ultimos dados validos que tinha (nunca derruba).
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import ndd_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("main")
logging.getLogger("zeep").setLevel(logging.WARNING)

# Numero maximo de instituicoes buscadas em paralelo ao mesmo tempo.
# Evita abrir chamadas demais de uma vez so contra a API da NDD.
MAX_CONCURRENT_ENTERPRISES = 10

# Fuso horario usado para calcular "hoje" na janela de contadores/paginas.
# Usar UTC puro fazia a virada de "dia" acontecer as 21h no horario de
# Brasilia (UTC-3) - bem no meio do horario comercial - zerando a janela
# de consumo (FirstCounterTotal ~= LatestCounterTotal) justamente quando
# ainda havia bastante movimento no dia.
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")

# =============================================================================
# Cache em memoria - a "fonte de verdade" inteira da aplicacao vive aqui.
# =============================================================================
cache: dict = {
    "printers": [],
    "events": [],
    "stocks": [],
    "stats": {"total": 0, "online": 0, "offline": 0, "alerts": 0},
    "last_updated": None,
    "error": None,
    "loading": False,
    "loading_progress": {"done": 0, "total": 0},
}

_poll_task: asyncio.Task | None = None
_cache_lock = asyncio.Lock()

# Flag para so logar o formato bruto do LastNSLDate UMA vez por processo
# (evita poluir o log a cada ciclo de polling depois que ja confirmamos
# o formato). Pode remover a variavel e o bloco de log associado depois
# que o formato estiver confirmado e o parser ajustado definitivamente.
_logged_raw_last_nsl_sample = False

# Reconhece o formato de data estilo .NET/JSON antigo: "/Date(1735891200000)/"
# (milissegundos desde epoch, opcionalmente com offset tipo "+0000" no final).
_DOTNET_DATE_RE = re.compile(r"^/Date\((-?\d+)\)")

# Formatos de string conhecidos que a NDD (ou variantes de WSDL/regiao)
# podem usar para o LastNSLDate. Tentados em ordem ate um funcionar.
_KNOWN_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",       # ISO 8601: 2026-08-03T20:45:00
    "%Y-%m-%d %H:%M:%S",       # 2026-08-03 20:45:00
    "%d/%m/%Y %H:%M:%S",       # 03/08/2026 20:45:00 (BR)
    "%m/%d/%Y %H:%M:%S",       # 08/03/2026 20:45:00 (US, 24h)
    "%m/%d/%Y %I:%M:%S %p",    # 08/03/2026 08:45:00 PM (US, 12h)
    "%d/%m/%Y %I:%M:%S %p",    # 03/08/2026 08:45:00 PM (BR, 12h)
)


def _parse_ndd_datetime(raw: str) -> datetime | None:
    """Tenta converter o valor bruto de data devolvido pela NDD (formato
    nem sempre consistente entre metodos/regioes) para um datetime com
    timezone UTC. Devolve None se nenhum formato conhecido bater."""
    text = raw.strip()
    if not text:
        return None

    # Formato .NET antigo: "/Date(1735891200000)/"
    m = _DOTNET_DATE_RE.match(text)
    if m:
        millis = int(m.group(1))
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)

    # ISO 8601 com offset/timezone (ex: 2026-08-03T20:45:00-03:00 ou
    # terminando em "Z"). fromisoformat nao aceita "Z" diretamente.
    try:
        iso_text = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso_text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    # Formatos fixos conhecidos, tentando so os primeiros N caracteres
    # relevantes de cada formato (cobre casos com milissegundos/lixo
    # sobrando no final, ex: "2026-08-03T20:45:00.123").
    for fmt in _KNOWN_DATE_FORMATS:
        # Aproxima o tamanho esperado da string pro formato, mas tenta
        # o texto inteiro tambem, caso a heuristica de corte erre.
        for candidate in (text[:19], text):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    return None


def _is_online(last_nsl_date_str: str | None) -> bool:
    """Proxy de online/offline: a API do NDD nao expoe um campo binario
    direto, entao usamos a data da ultima leitura de contador."""
    global _logged_raw_last_nsl_sample
    if not _logged_raw_last_nsl_sample and last_nsl_date_str:
        logger.info("DEBUG LastNSLDate - valor bruto de amostra: %r", last_nsl_date_str)
        _logged_raw_last_nsl_sample = True

    if not last_nsl_date_str:
        return False

    last = _parse_ndd_datetime(str(last_nsl_date_str))
    if last is None:
        logger.warning(
            "LastNSLDate em formato nao reconhecido, tratando como offline: %r",
            last_nsl_date_str,
        )
        return False

    age_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return age_minutes <= config.PRINTER_OFFLINE_THRESHOLD_MINUTES


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value) -> bool:
    """A API do NDD pode devolver booleanos como True/False reais ou
    como string 'true'/'false' dependendo do metodo - trata os dois casos."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "sim"}
    return bool(value)


# Nomes de campo que a API pode usar para o endereco IP da impressora.
# Confirmado na doc oficial (getprinters-atualizada): o campo real e
# "AddressName" ("IP da impressora"). Mantemos os outros nomes como
# fallback caso o provedor use um WSDL/versao diferente.
_PRINTER_IP_KEYS = (
    "AddressName", "IPAddress", "IpAddress", "PrinterIPAddress", "PrinterIpAddress",
    "IP", "Ip", "PrinterIP", "NetworkAddress", "HostAddress",
)


def _first_present(d: dict, keys: tuple[str, ...]):
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return None


def _recompute_stats() -> None:
    """Recalcula os KPIs a partir do que ja esta em cache["printers"].
    Chamado apos cada instituicao terminar, para que a tela va
    atualizando progressivamente em vez de tudo de uma vez no final."""
    printers = cache["printers"]
    online_count = sum(1 for p in printers if p["is_online"])
    suspended_count = sum(1 for p in printers if p.get("is_suspended"))
    cache["stats"] = {
        "total": len(printers),
        "online": online_count,
        "offline": len(printers) - online_count,
        "suspended": suspended_count,
        "alerts": len(cache.get("events", [])),
        "pages_total": sum(p["total_consumed"] for p in printers),
        "pages_mono": sum(p["mono_consumed"] for p in printers),
        "pages_color": sum(p["color_consumed"] for p in printers),
    }


async def _fetch_enterprise_printers(ent: dict) -> list[dict]:
    """Busca e normaliza as impressoras de UMA instituicao. Nao mexe no
    cache diretamente - devolve a lista pronta para o chamador decidir
    quando/como mesclar (permite paralelismo com asyncio.gather)."""
    ent_id = _safe_int(ent.get("EnterpriseID"))
    if not ent_id:
        return []
    try:
        printers = await asyncio.to_thread(ndd_client.get_printers, ent_id)
    except Exception:
        logger.warning("Falha ao buscar impressoras da instituicao %s", ent.get("EnterpriseName"))
        return []

    for p in printers:
        p["EnterpriseName"] = ent.get("EnterpriseName")
        p["_uid"] = f"{ent_id}-{p.get('PrinterDeviceID')}"
        p["is_online"] = _is_online(p.get("LastNSLDate"))
        p["is_suspended"] = _safe_bool(p.get("IsSuspended", False))
        p["mono_consumed"] = 0
        p["color_consumed"] = 0
        p["total_consumed"] = 0
        p["ip_address"] = _first_present(p, _PRINTER_IP_KEYS)
        p["toner_level"] = None
        p["toner_detail"] = []
        p["fotocondutor_level"] = None
        p["fotocondutor_detail"] = []
    return printers


async def _fetch_enterprise_sites(ent: dict) -> list[dict]:
    """Busca os sites/unidades de UMA instituicao. Nao e usado para
    exibir nada diretamente - serve so para montar um mapa
    SiteID -> instituicao, usado como fallback para vincular cada
    estoque (GetDealerStocks) a uma instituicao quando a API nao manda
    esse vinculo direto no proprio estoque."""
    ent_id = _safe_int(ent.get("EnterpriseID"))
    if not ent_id:
        return []
    try:
        sites = await asyncio.to_thread(ndd_client.get_sites, ent_id)
    except Exception:
        logger.warning("Falha ao buscar sites da instituicao %s", ent.get("EnterpriseName"))
        return []
    for s in sites:
        s["EnterpriseID"] = ent_id
        s["EnterpriseName"] = ent.get("EnterpriseName")
    return sites


# Nomes de campo que a API pode usar para indicar a instituicao dona de
# um estoque, ou o site ao qual ele pertence. Tentamos varias
# possibilidades porque a doc oficial nao deixa 100% claro qual delas
# o GetDealerStocks realmente devolve.
_STOCK_ENTERPRISE_NAME_KEYS = ("EnterpriseName", "ClientName", "CustomerName", "CompanyName")
_STOCK_ENTERPRISE_ID_KEYS = ("EnterpriseID", "EnterpriseId", "ClientID", "CustomerID")
_STOCK_SITE_ID_KEYS = ("SiteID", "SiteId")


def _attach_enterprise_to_stock(
    stock: dict, enterprise_by_id: dict[int, str], site_enterprise_map: dict[int, str]
) -> dict:
    """Descobre a qual instituicao um estoque pertence, tentando nessa ordem:
    1) campo de nome direto no proprio estoque (ex: EnterpriseName);
    2) campo de ID direto no proprio estoque, cruzado com a lista de instituicoes;
    3) SiteID do estoque, cruzado com o mapa SiteID -> instituicao (fallback).
    Se nada disso resolver, marca como "Sem instituicao vinculada" em vez
    de quebrar o agrupamento no frontend."""
    name = _first_present(stock, _STOCK_ENTERPRISE_NAME_KEYS)

    if not name:
        ent_id = _first_present(stock, _STOCK_ENTERPRISE_ID_KEYS)
        if ent_id is not None:
            name = enterprise_by_id.get(_safe_int(ent_id))

    if not name:
        site_id = _first_present(stock, _STOCK_SITE_ID_KEYS)
        if site_id is not None:
            name = site_enterprise_map.get(_safe_int(site_id))

    stock["EnterpriseName"] = name or "Sem instituicao vinculada"
    return stock


# Cada categoria de suprimento que queremos mostrar na aba Toner, com as
# palavras-chave (case-insensitive, substring) usadas pra reconhecer o
# SupplySubFunctionTypeName daquele slot. RETRIEVEACTIVESUPPLIES devolve
# TODOS os tipos de suprimento junto (toner, fotocondutor/cilindro,
# fusor, coletor de residuo etc) - sem esse filtro por categoria, tudo
# ficaria misturado num unico "nivel" sem sentido.
# Pra adicionar um novo suprimento na tela (ex: fusor), so acrescentar
# uma linha aqui - o resto (merge, coluna na tabela) e generico.
_SUPPLY_CATEGORIES = {
    # "ink cartridge" cobre impressoras inkjet (ex: Epson WorkForce), que
    # reportam o consumivel de impressao como "Ink Cartridge" em vez de
    # "Toner Cartridge". Usamos a frase completa (nao so "ink") pra nao
    # capturar por engano o "Waste Ink" (coletor de residuo, sem relacao
    # com nivel de tinta/toner de impressao).
    "toner": ("toner", "ink cartridge"),
    "fotocondutor": ("fotocondutor", "photoconductor", "drum", "cilindro", "imaging unit", "unidade de imagem"),
}


def _apply_supplies_to_printers(supplies: list[dict], printers: list[dict]) -> None:
    """Cruza os suprimentos ativos (RETRIEVEACTIVESUPPLIES) com as
    impressoras ja carregadas em `printers`, preenchendo, pra cada
    categoria em _SUPPLY_CATEGORIES, os campos "<categoria>_level" (o
    pior percentual entre os slots daquele tipo, na impressora) e
    "<categoria>_detail" (percentual por cor/slot, pra detalhar na tela).

    Casa por EnterpriseID+PrinterDeviceID (mesmo _uid usado pros
    contadores) - cada impressora pode ter varios slots (um por cor,
    ou um por tipo de suprimento), entao agrupa tudo antes de decidir
    o pior caso de cada categoria."""
    printers_by_uid = {p["_uid"]: p for p in printers}

    # by_printer[uid][categoria] = lista de itens daquele tipo, pra essa impressora
    by_printer: dict[str, dict[str, list[dict]]] = {}
    for s in supplies:
        ent_id = _safe_int(s.get("EnterpriseID"), None)
        device_id = s.get("PrinterDeviceID")
        if ent_id is None or device_id is None:
            continue
        type_name = str(s.get("SupplySubFunctionTypeName") or "").lower()
        category = next(
            (cat for cat, keywords in _SUPPLY_CATEGORIES.items() if any(kw in type_name for kw in keywords)),
            None,
        )
        if category is None:
            continue
        uid = f"{ent_id}-{device_id}"
        by_printer.setdefault(uid, {}).setdefault(category, []).append(s)

    for uid, categories in by_printer.items():
        printer = printers_by_uid.get(uid)
        if not printer:
            continue
        for category, items in categories.items():
            levels = []
            detail = []
            for s in items:
                pct = _safe_float(s.get("LatestLoadStatus"))
                if pct is None:
                    continue
                levels.append(pct)
                detail.append({"color": s.get("SupplyColorName") or category.title(), "level": pct})
            if levels:
                printer[f"{category}_level"] = min(levels)
                printer[f"{category}_detail"] = detail


def _enrich_events_with_printer_info(events: list[dict], printers: list[dict]) -> list[dict]:
    """Cruza cada evento com a impressora correspondente (ja em cache) para
    trazer IP, nome da instituicao e nome/modelo da impressora pro feed.

    Tenta casar por EnterpriseID+PrinterDeviceID (igual ao _uid usado no
    cruzamento de contadores - sem ambiguidade) e, se o evento nao tiver
    EnterpriseID, cai para casar so por PrinterDeviceID (funciona quase
    sempre, mas pode raramente colidir se dois devices com o mesmo ID
    existirem em instituicoes diferentes)."""
    printers_by_uid = {p["_uid"]: p for p in printers}
    printers_by_device_id: dict[str, dict] = {}
    for p in printers:
        device_id = str(p.get("PrinterDeviceID"))
        printers_by_device_id.setdefault(device_id, p)

    for ev in events:
        device_id = ev.get("PrinterDeviceID")
        ent_id = _safe_int(ev.get("EnterpriseID"), None)
        printer = None
        if ent_id and device_id is not None:
            printer = printers_by_uid.get(f"{ent_id}-{device_id}")
        if not printer and device_id is not None:
            printer = printers_by_device_id.get(str(device_id))

        ev["ip_address"] = printer.get("ip_address") if printer else None
        ev["EnterpriseName"] = printer.get("EnterpriseName") if printer else ev.get("EnterpriseName")
        ev["PrinterModel"] = (printer.get("PrinterModel") or printer.get("Model")) if printer else None
    return events


async def refresh_data() -> None:
    """Busca tudo de novo na API do NDD e atualiza o cache global.

    PARALELO: as instituicoes sao buscadas ate MAX_CONCURRENT_ENTERPRISES
    por vez, em vez de uma de cada vez.
    NAO-DESTRUTIVO: a coleta e montada numa lista local (`new_printers`),
    SEM tocar em cache["printers"]. O dashboard continua servindo o
    ultimo snapshot completo e valido (via /api/dashboard) ate a coleta
    nova terminar 100% - so entao ela e publicada de uma vez.
    Antes, cache["printers"] era zerado e reconstruido ao vivo a cada
    ciclo (nao so no primeiro load); qualquer poll do frontend que
    batesse no meio da reconstrucao via os KPIs e a lista de impressoras
    "regredindo" temporariamente (e o feed de eventos, montado so depois
    que as impressoras terminam, ficava desalinhado ate o ciclo fechar).
    """
    logger.info("Iniciando coleta de dados na API do NDD Print...")
    cache["loading"] = True
    try:
        enterprises = await asyncio.to_thread(ndd_client.get_enterprises)
        logger.info(
            "RESUMO: %d instituicoes encontradas: %s",
            len(enterprises), [e.get("EnterpriseName") for e in enterprises],
        )

        cache["loading_progress"] = {"done": 0, "total": len(enterprises)}
        new_printers: list[dict] = []

        # Mapas usados so para vincular o estoque (que nao tem endpoint
        # proprio por instituicao) a instituicao correta:
        #   - enterprise_by_id: EnterpriseID -> EnterpriseName (opcao 1)
        #   - site_enterprise_map: SiteID -> EnterpriseName (opcao 2, fallback)
        enterprise_by_id = {
            _safe_int(e.get("EnterpriseID")): e.get("EnterpriseName") for e in enterprises
        }
        site_enterprise_map: dict[int, str] = {}

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ENTERPRISES)

        async def bounded_fetch(ent: dict) -> None:
            async with semaphore:
                printers, sites = await asyncio.gather(
                    _fetch_enterprise_printers(ent),
                    _fetch_enterprise_sites(ent),
                )
                async with _cache_lock:
                    new_printers.extend(printers)
                    for s in sites:
                        site_id = _safe_int(s.get("SiteID"))
                        if site_id:
                            site_enterprise_map[site_id] = s.get("EnterpriseName")
                    cache["loading_progress"]["done"] += 1
                    # NAO chama _recompute_stats() aqui - cache["printers"]
                    # ainda e o snapshot antigo, que deve continuar intacto
                    # pro frontend ate a troca atomica la embaixo.
                logger.info(
                    "RESUMO: instituicao %s -> %d impressoras, %d sites (%d/%d instituicoes)",
                    ent.get("EnterpriseName"), len(printers), len(sites),
                    cache["loading_progress"]["done"], len(enterprises),
                )

        await asyncio.gather(*[bounded_fetch(ent) for ent in enterprises])

        # Contadores, eventos e estoque nao dependem uns dos outros -
        # buscar os tres em paralelo. IMPORTANTE: isso acontece ANTES da
        # troca atomica do cache["printers"] (mais abaixo). Antes, os
        # contadores eram mesclados DEPOIS de ja ter publicado
        # new_printers (com total_consumed=0 "de fabrica") no cache -
        # isso deixava uma janela real, a cada ciclo, em que qualquer
        # poll do frontend via "Paginas (hoje)" zerado ate os contadores
        # chegarem e serem mesclados. Buscando tudo antes e mesclando em
        # new_printers (lista local) ainda antes da troca atomica, o
        # cache so e publicado ja com os numeros de consumo corretos -
        # sem oscilar entre zero e o valor real a cada ciclo.
        now_brazil = datetime.now(BRAZIL_TZ)
        window_start = now_brazil.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = now_brazil

        counters_task = asyncio.create_task(
            asyncio.to_thread(ndd_client.get_plain_counters_data, window_start, window_end)
        )
        events_task = asyncio.create_task(asyncio.to_thread(ndd_client.get_open_events))
        stocks_task = asyncio.create_task(asyncio.to_thread(ndd_client.get_stocks))
        supplies_task = asyncio.create_task(asyncio.to_thread(ndd_client.get_active_supplies))

        try:
            counters = await counters_task
            counters_by_uid = {
                f"{_safe_int(c.get('EnterpriseID'))}-{_safe_int(c.get('PrinterDeviceID'))}": c
                for c in counters
            }
            for p in new_printers:
                c = counters_by_uid.get(p["_uid"])
                if not c:
                    continue
                first_total = _safe_int(c.get("FirstCounterTotal"), None)
                latest_total = _safe_int(c.get("LatestCounterTotal"), None)
                first_mono = _safe_int(c.get("FirstCounterMono"), None)
                latest_mono = _safe_int(c.get("LatestCounterMono"), None)
                first_color = _safe_int(c.get("FirstCounterColor"), None)
                latest_color = _safe_int(c.get("LatestCounterColor"), None)
                p["total_consumed"] = max((latest_total or 0) - (first_total or 0), 0)
                p["mono_consumed"] = max((latest_mono or 0) - (first_mono or 0), 0)
                p["color_consumed"] = max((latest_color or 0) - (first_color or 0), 0)
        except Exception as exc:
            logger.warning("Falha ao buscar contadores - seguindo sem dados de consumo desta rodada: %s", exc)

        try:
            events = await events_task
            if events:
                # DEBUG: mostra os campos que a API devolveu no primeiro
                # evento - util pra confirmar se EnterpriseID vem junto
                # (permite cruzamento exato) ou so PrinterDeviceID
                # (cruzamento por fallback). Pode remover depois de conferir.
                logger.info("DEBUG evento - campos disponiveis: %s", sorted(events[0].keys()))
            new_events = _enrich_events_with_printer_info(events, new_printers)
        except Exception as exc:
            logger.warning("Falha ao buscar eventos: %s", exc)
            new_events = cache["events"]  # mantem o feed anterior em vez de zerar

        try:
            stocks = await stocks_task
            if stocks:
                # DEBUG: mostra os campos que a API realmente devolveu no
                # primeiro estoque - util pra confirmar se o vinculo direto
                # com a instituicao (opcao 1) existe ou nao. Pode remover
                # esta linha depois de conferir no log.
                logger.info("DEBUG estoque - campos disponiveis: %s", sorted(stocks[0].keys()))
            new_stocks = [
                _attach_enterprise_to_stock(s, enterprise_by_id, site_enterprise_map) for s in stocks
            ]
        except Exception as exc:
            logger.warning("Falha ao buscar estoque/suprimentos: %s", exc)
            new_stocks = cache["stocks"]  # mantem o estoque anterior em vez de zerar

        try:
            supplies = await supplies_task
            if supplies:
                # DEBUG: mostra os campos que a API devolveu no primeiro
                # suprimento - util pra confirmar os nomes reais dos campos
                # de tipo/cor/percentual. Pode remover depois de conferir.
                logger.info("DEBUG suprimento - campos disponiveis: %s", sorted(supplies[0].keys()))
            _apply_supplies_to_printers(supplies, new_printers)
        except Exception as exc:
            # Nao falha a coleta inteira por causa disso - so deixa
            # toner_level como None (frontend ja mostra "sem dado").
            logger.warning("Falha ao buscar nivel de toner (RETRIEVEACTIVESUPPLIES): %s", exc)

        # Troca atomica: só agora o novo snapshot completo - impressoras,
        # eventos, contadores E estoque, tudo ja mesclado - substitui o
        # antigo de uma vez. Ate aqui, /api/dashboard servia os dados
        # antigos (validos) o tempo todo - sem regressao visivel, inclusive
        # sem a janela de "Paginas (hoje)" zerada que existia antes.
        cache["printers"] = new_printers
        cache["events"] = new_events
        cache["stocks"] = new_stocks
        _recompute_stats()

        if cache["printers"]:
            # DEBUG: mostra os campos que a API devolveu na primeira
            # impressora - util pra confirmar o nome real do campo de IP.
            # Pode remover esta linha depois de conferir no log.
            logger.info("DEBUG impressora - campos disponiveis: %s", sorted(cache["printers"][0].keys()))
            logger.info("DEBUG impressora - ip_address encontrado: %r", cache["printers"][0].get("ip_address"))
            logger.info(
                "DEBUG impressora - LastNSLDate/is_online da amostra: %r -> %r",
                cache["printers"][0].get("LastNSLDate"), cache["printers"][0].get("is_online"),
            )

        cache["last_updated"] = datetime.now(timezone.utc).isoformat()
        cache["error"] = None
        toner_count = sum(1 for p in cache["printers"] if p.get("toner_level") is not None)
        drum_count = sum(1 for p in cache["printers"] if p.get("fotocondutor_level") is not None)
        logger.info(
            "Coleta concluida: %d impressoras (%d online), %d eventos, %d estoques, "
            "%d com nivel de toner, %d com nivel de fotocondutor",
            cache["stats"]["total"], cache["stats"]["online"],
            len(cache["events"]), len(cache["stocks"]), toner_count, drum_count,
        )
    except Exception as exc:  # noqa: BLE001
        # Nunca deixa a excecao subir e derrubar o loop/servidor.
        logger.exception("Falha na coleta de dados do NDD Print")
        cache["error"] = str(exc)
    finally:
        cache["loading"] = False


async def _poll_loop() -> None:
    while True:
        await refresh_data()
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _poll_task
    if not config.NDD_DEALER_NAME or not config.NDD_DEALER_PASSWORD:
        logger.warning(
            "NDD_DEALER_NAME / NDD_DEALER_PASSWORD nao configurados no .env - "
            "as chamadas a API do NDD vao falhar ate isso ser corrigido."
        )
    _poll_task = asyncio.create_task(_poll_loop())
    yield
    if _poll_task:
        _poll_task.cancel()


app = FastAPI(title="NDD Print NOC Monitor (versao simples)", lifespan=lifespan)

# Caminho absoluto baseado na localizacao deste arquivo (main.py), e nao
# no diretorio de onde o comando `uvicorn` foi executado. Sem isso, rodar
# o servidor de fora da pasta do projeto (ex: outro diretorio de trabalho,
# um servico do Windows, uma task agendada) quebra o /static e o "/" com
# um erro 404/500 procurando "static/index.html" em outro lugar.
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/dashboard")
async def get_dashboard() -> JSONResponse:
    """Endpoint unico que o frontend consulta via polling (setInterval).
    Pode ser consultado a qualquer momento durante a coleta - os dados
    vem parciais/crescendo enquanto cache["loading"] for True."""
    return JSONResponse(cache)


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "last_updated": cache["last_updated"],
        "has_error": cache["error"] is not None,
        "loading": cache["loading"],
    }


@app.post("/api/refresh")
async def force_refresh() -> dict:
    """Dispara uma coleta imediata, sem esperar o proximo ciclo do polling."""
    asyncio.create_task(refresh_data())
    return {"status": "atualizacao iniciada em segundo plano"}
