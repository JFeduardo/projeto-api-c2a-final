"""
config.py
=========
Configuracao simples via variaveis de ambiente. Sem pydantic-settings,
sem classes de config elaboradas — so `os.environ.get(...)` com
valores padrao sensatos.

Carrega o arquivo .env (se existir) usando python-dotenv, depois le
tudo com os.environ.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv(override=True)  # valores do .env sempre vencem, mesmo se ja existir a variavel vazia na sessao

# ------------------------------ NDD Print -----------------------------------
NDD_DEALER_NAME = os.environ.get("NDD_DEALER_NAME", "")
NDD_DEALER_USER_EMAIL = os.environ.get("NDD_DEALER_USER_EMAIL", "")
NDD_DEALER_PASSWORD = os.environ.get("NDD_DEALER_PASSWORD", "")

# WSDLs dos Web Services (regiao GLOBAL por padrao — troque o host se o
# seu provedor estiver em outra regiao: GLOBAL2 / USA / EURO)
NDD_GENERAL_WSDL = os.environ.get(
    "NDD_GENERAL_WSDL", "https://api-general.nddprint.com/GeneralWS/GeneralData2.asmx?wsdl"
)
NDD_COUNTERS_WSDL = os.environ.get(
    "NDD_COUNTERS_WSDL", "https://api-counters.nddprint.com/CountersWS/CountersData.asmx?wsdl"
)
NDD_EVENTS_WSDL = os.environ.get(
    "NDD_EVENTS_WSDL", "https://api-events.nddprint.com/EventsWS/EventsData.asmx?wsdl"
)
# OnDemandWS - usado hoje so para o nivel de toner/suprimentos
# (RETRIEVEACTIVESUPPLIES). Endpoint separado do GeneralData2.
NDD_ONDEMAND_WSDL = os.environ.get(
    "NDD_ONDEMAND_WSDL", "https://api-general.nddprint.com/OnDemandWS/OnDemandData.asmx?wsdl"
)

SOAP_TIMEOUT_SECONDS = int(os.environ.get("SOAP_TIMEOUT_SECONDS", "30"))

# ------------------------------ Polling --------------------------------------
# Intervalo (em segundos) entre cada ciclo de atualizacao dos dados.
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))

# Impressora e considerada offline se nao tiver leitura de contador ha
# mais que N minutos (a API do NDD nao expoe um campo online/offline
# binario direto - ver README).
PRINTER_OFFLINE_THRESHOLD_MINUTES = int(os.environ.get("PRINTER_OFFLINE_THRESHOLD_MINUTES", "30"))
