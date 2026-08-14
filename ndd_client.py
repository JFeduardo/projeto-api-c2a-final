from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import zeep
from requests import Session
from requests.adapters import HTTPAdapter
from zeep.transports import Transport

import config

logger = logging.getLogger("ndd_client")

# Cache de clientes zeep (o parsing do WSDL e caro, entao cada WSDL so
# e carregado uma vez e reaproveitado nas chamadas seguintes).
_zeep_clients: dict[str, zeep.Client] = {}

# Tamanho do pool de conexoes HTTP por host. O requests usa 10 por padrao,
# que e menor que MAX_CONCURRENT_ENTERPRISES (main.py busca ate 10
# instituicoes em paralelo) - isso fazia o pool "estourar" e descartar
# conexoes no meio das chamadas (log: "Connection pool is full, discarding
# connection"), o que pode causar timeouts/reconexoes silenciosas.
_HTTP_POOL_SIZE = 20


def _get_zeep_client(wsdl_url: str) -> zeep.Client:
    if wsdl_url not in _zeep_clients:
        session = Session()
        adapter = HTTPAdapter(pool_connections=_HTTP_POOL_SIZE, pool_maxsize=_HTTP_POOL_SIZE)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        transport = Transport(timeout=config.SOAP_TIMEOUT_SECONDS, session=session)
        _zeep_clients[wsdl_url] = zeep.Client(wsdl=wsdl_url, transport=transport)
    return _zeep_clients[wsdl_url]


def _credentials() -> dict:
    return {
        "DealerName": config.NDD_DEALER_NAME,
        "DealerUserEmail": config.NDD_DEALER_USER_EMAIL,
        "DealerUserPassword": config.NDD_DEALER_PASSWORD,
    }


def _parse_response(raw: Any) -> Any:
    """A maioria dos metodos do GeneralData2 devolve uma string JSON.
    Metodos mais antigos (Counters/Events) podem devolver XML/lista direta.
    Tentamos JSON; se falhar, devolvemos o valor bruto."""
    if raw is None:
        return []
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _as_list(result: Any) -> list[dict]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("Items", "Result", "Data", "Rows"):
            if key in result and isinstance(result[key], list):
                return result[key]
        return [result]
    return []


def _call_general(method_name: str, extra_properties: dict | None = None) -> list[dict]:
    """Chama um metodo do GeneralData2 (recebe um unico parametro `properties` = JSON string)."""
    client = _get_zeep_client(config.NDD_GENERAL_WSDL)
    payload = _credentials()
    if extra_properties:
        payload.update({k: v for k, v in extra_properties.items() if v is not None})
    try:
        raw = getattr(client.service, method_name)(json.dumps(payload))
        return _as_list(_parse_response(raw))
    except Exception as exc:
        logger.warning("Falha ao chamar GeneralData2.%s: %s", method_name, exc)
        raise


def _call_positional(wsdl_url: str, method_name: str, *args: Any) -> list[dict]:
    """Chama um metodo com parametros posicionais soltos (CountersData/EventsData),
    incluindo as credenciais automaticamente no inicio da lista de argumentos."""
    client = _get_zeep_client(wsdl_url)
    all_args = (config.NDD_DEALER_NAME, config.NDD_DEALER_USER_EMAIL, config.NDD_DEALER_PASSWORD, *args)
    try:
        raw = getattr(client.service, method_name)(*all_args)
        return _as_list(_parse_response(raw))
    except Exception as exc:
        logger.warning("Falha ao chamar %s: %s", method_name, exc)
        raise


# =============================================================================
# GeneralDataWS — instituicoes, sites, departamentos, impressoras, suprimentos
# =============================================================================

def get_enterprises() -> list[dict]:
    """Lista as instituicoes (empresas clientes) do provedor."""
    return _call_general("GetEnterprises")


def get_sites(enterprise_id: int) -> list[dict]:
    """Lista os sites/unidades de uma instituicao."""
    return _call_general("GetSites", {"EnterpriseID": enterprise_id})


def get_site_divisions(site_id: int) -> list[dict]:
    """Lista os departamentos de um site."""
    return _call_general("GetSiteDivisions", {"SiteID": site_id})


# Campos pedidos explicitamente ao GetPrinters. Sem informar FieldsList, a
# API devolve so os campos "em negrito" da documentacao (PrinterDeviceID,
# PrinterName, SerialNumber) - SEM IP, SEM Location e SEM LastNSLDate. Isso
# fazia o ip_address ficar sempre None, a coluna "Local" ficar vazia e TODAS
# as impressoras aparecerem como offline (LastNSLDate ausente = _is_online
# sempre False). Ver: helpcenter.nddprint.com/.../getprinters-atualizada
_PRINTER_FIELDS_LIST = (
    "PrinterDeviceID;PrinterName;BrandName;ModelName;SerialNumber;"
    "AddressName;AddressMac;Location;SiteName;SiteDivisionName;"
    "IsSuspended;LastNSLDate;Engaged;EnabledBilling;EnabledCounters"
)


def _is_printer_ativa(printer: dict) -> bool:
    """
    Considera "ativa" (ainda em operacao real, nao uma sobra no cadastro
    da NDD) a impressora que atende AMBAS as condicoes abaixo:

    - EnabledCounters == True  -> monitoramento de contadores habilitado
    - IsSuspended != True      -> nao esta suspensa

    Removido o criterio Engaged (impressora "de contrato"): algumas
    instituicoes zeraram 100% das impressoras com esse criterio (ex:
    712 de 712, 326 de 326) - sinal de que Engaged reflete o modelo
    comercial do cliente com a NDD (contrato formal vs avulso/pos-pago),
    nao se a impressora esta realmente em operacao. Usa-lo descartaria
    parques inteiros de clientes legitimos.

    Documentado no Integration Kit da NDD (GeneralData2.GetPrinters):
    resources.nddprint.com/docs/api/integrationkit/pt-br/
    """
    return (
        printer.get("EnabledCounters") is True
        and printer.get("IsSuspended") is not True
    )


def get_printers(enterprise_id: int, apenas_ativas: bool = True) -> list[dict]:
    """
    Lista as impressoras de uma instituicao (com IP, local e status).

    apenas_ativas=True (padrao) filtra fora impressoras fora de contrato,
    com contadores desabilitados ou suspensas - que costumam ser "sobras"
    no cadastro da NDD que nunca foram removidas, e nao devem contar como
    parque instalado real. Passe False para ver a lista completa (auditoria).
    """
    printers = _call_general(
        "GetPrinters",
        {"EnterpriseID": enterprise_id, "FieldsList": _PRINTER_FIELDS_LIST},
    )
    if not apenas_ativas:
        return printers

    total = len(printers)
    ativas = [p for p in printers if _is_printer_ativa(p)]
    if total != len(ativas):
        logger.info(
            "get_printers(enterprise_id=%s): %d de %d impressoras filtradas "
            "(fora de contrato / contadores desabilitados / suspensas)",
            enterprise_id, total - len(ativas), total,
        )
    return ativas


def get_printer_slots(enterprise_id: int, printer_device_id: int) -> list[dict]:
    """Lista os slots de suprimento (cor/tipo) de uma impressora especifica."""
    return _call_general(
        "GetPrintersSlots", {"EnterpriseID": enterprise_id, "PrinterDeviceID": printer_device_id}
    )


# StockIDs que ja falharam com ERR_ACCESS_USERSTOCK (ou qualquer outro
# erro) em algum ciclo anterior. Uma vez que um estoque falha, e muito
# provavel que seja permissao (nao vai resolver sozinho de um ciclo pro
# outro), entao evitamos tentar de novo pra nao gastar chamadas a toa.
# Fica em memoria (reseta se o processo reiniciar).
_known_failing_stock_ids: set[Any] = set()

# Se X chamadas SEGUIDAS falharem dentro do MESMO ciclo (estoques ainda
# nao conhecidos como falhos), para de tentar as restantes deste ciclo.
# Protege contra rajadas que estourem o limite de chamadas da conta
# (ERR_ACCESS_LIMIT), que aí derrubaria tambem eventos/contadores que
# rodam em paralelo.
_MAX_CONSECUTIVE_STOCK_ITEM_FAILURES = 3


def get_stocks() -> list[dict]:
    """
    Suprimentos/estoque. Combina GetDealerStocks (lista de estoques) com
    GetTotalStockItemsAvailable (saldo por estoque) para devolver uma
    lista "achatada" de estoques, cada um ja com seus itens embutidos.

    Pula estoques ja conhecidos como falhos (ver _known_failing_stock_ids)
    e para de tentar os restantes do ciclo se detectar uma rajada de
    falhas seguidas (ver _MAX_CONSECUTIVE_STOCK_ITEM_FAILURES), pra nao
    gastar a cota de chamadas da API a toa.
    """
    stocks = _call_general("GetDealerStocks")
    consecutive_failures = 0
    breaker_tripped = False
    skipped_known = 0
    skipped_breaker = 0

    for stock in stocks:
        stock_id = stock.get("StockID")
        if not stock_id:
            stock["Items"] = []
            continue

        if stock_id in _known_failing_stock_ids:
            stock["Items"] = []
            skipped_known += 1
            continue

        if breaker_tripped:
            stock["Items"] = []
            skipped_breaker += 1
            continue

        try:
            stock["Items"] = _call_general("GetTotalStockItemsAvailable", {"StockID": stock_id})
            consecutive_failures = 0
        except Exception:
            stock["Items"] = []
            _known_failing_stock_ids.add(stock_id)
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_STOCK_ITEM_FAILURES:
                breaker_tripped = True

    if skipped_known or skipped_breaker:
        logger.info(
            "get_stocks: %d estoque(s) pulado(s) por falha conhecida, %d pulado(s) pelo disjuntor "
            "(%d falhas seguidas) - poupando chamadas a API",
            skipped_known, skipped_breaker, _MAX_CONSECUTIVE_STOCK_ITEM_FAILURES,
        )
    return stocks


# =============================================================================
# CountersDataWS — contadores de paginas (papel)
# =============================================================================

def get_plain_counters_data(date_start: datetime, date_end: datetime) -> list[dict]:
    """Contadores (total/mono/color) de todas as impressoras no periodo informado."""
    fmt = "%Y-%m-%d %H:%M:%S"
    # Sem informar fieldsList, a API so devolve os campos padrao (em negrito
    # na doc oficial) - que NAO incluem os contadores. Isso fazia
    # FirstCounterTotal/LatestCounterTotal etc sempre virem ausentes, o
    # cruzamento por _uid no main.py nunca achar dado, e "Paginas (hoje)"
    # ficar sempre zerado. Mesmo problema que ja tinhamos visto no
    # GetPrinters (IP/LastNSLDate sumindo sem FieldsList explicito).
    fields_list = (
        "EnterpriseID;PrinterDeviceID;"
        "FirstCounterTotal;LatestCounterTotal;"
        "FirstCounterMono;LatestCounterMono;"
        "FirstCounterColor;LatestCounterColor"
    )
    return _call_positional(
        config.NDD_COUNTERS_WSDL,
        "GetPlainCountersData",
        date_start.strftime(fmt),
        date_end.strftime(fmt),
        90,           # maxLimitDaysEarlier
        "",           # enterpriseName (vazio = todas)
        "",           # serialNumber
        "",           # siteName
        "",           # siteDivisionName
        False,        # engaged (False = todas impressoras, nao so as de contrato)
        fields_list,  # fieldsList
    )


# =============================================================================
# EventsDataWS — eventos e alertas abertos
# =============================================================================

def get_open_events() -> list[dict]:
    """Eventos abertos (status 1=pendente + 2=aguardando resolucao)."""
    pending = _call_positional(config.NDD_EVENTS_WSDL, "GetPrintersEvents", 1)
    awaiting = _call_positional(config.NDD_EVENTS_WSDL, "GetPrintersEvents", 2)
    return [*pending, *awaiting]
