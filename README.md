# NDD Print · NOC Monitor (versão simples)

Monitoramento de impressoras integrado à API do NDD Print. **Sem banco
de dados, sem ORM, sem Docker.** Tudo em memória, buscado ao vivo da
API a cada ciclo de polling.

## Arquitetura (4 arquivos de código)

```
ndd-noc-simple/
├── main.py          # FastAPI + loop de coleta em background + rotas
├── ndd_client.py     # Funções que chamam os Web Services SOAP do NDD
├── config.py          # Variáveis de ambiente (sem framework de config)
├── requirements.txt   # 4 dependências
├── .env.example
└── static/
    └── index.html      # Frontend inteiro (HTML+CSS+JS num arquivo só)
```

**Como funciona:** ao subir, o `main.py` dispara um loop `asyncio` que
a cada `POLL_INTERVAL_SECONDS` chama as funções de `ndd_client.py`,
processa o resultado e guarda tudo num dicionário Python global
(`cache`). O `index.html` faz `fetch('/api/dashboard')` a cada poucos
segundos (`setInterval` no JS) e redesenha a tela com o que tiver no
cache. Se uma coleta falhar, o erro fica registrado mas o servidor
continua rodando com os últimos dados válidos — nunca derruba.

## Instalação

```bash
python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env    # Windows: copy .env.example .env
# edite o .env e preencha NDD_DEALER_USER_EMAIL (obrigatório)
```

## Rodando

```bash
uvicorn main:app --reload
```

Abra **http://127.0.0.1:8000/** — pronto, sem banco, sem migração, sem
Docker.

## Endpoints

| Rota | O que faz |
|---|---|
| `GET /` | Serve o dashboard (`static/index.html`) |
| `GET /api/dashboard` | Retorna o cache atual (impressoras, eventos, estoque, stats) em JSON |
| `GET /api/health` | Status simples + horário da última coleta |
| `POST /api/refresh` | Força uma nova coleta imediatamente, sem esperar o próximo ciclo |

## Sobre os endpoints do NDD usados

A documentação oficial (`helpcenter.nddprint.com/en/ndd-print-integration-kit`)
não tem um `SuppliesDataWS` nem um método `GetOpenEvents` separados —
isso foi mapeado para os métodos reais confirmados:

| Função em `ndd_client.py` | Método SOAP real usado |
|---|---|
| `get_enterprises()` | `GeneralData2.GetEnterprises` |
| `get_sites()` / `get_site_divisions()` | `GeneralData2.GetSites` / `GetSiteDivisions` |
| `get_printers()` | `GeneralData2.GetPrinters` |
| `get_printer_slots()` | `GeneralData2.GetPrintersSlots` |
| `get_stocks()` | `GeneralData2.GetDealerStocks` + `GetTotalStockItemsAvailable` |
| `get_plain_counters_data()` | `CountersData.GetPlainCountersData` |
| `get_open_events()` | `EventsData.GetPrintersEvents` (status 1 + 2) |

## Limitações conhecidas da API (não são bugs deste código)

- **Nível percentual de toner** não é exposto por nenhum método
  documentado do NDD MPS.
- **Status online/offline** não existe como campo binário — este
  projeto usa a data da última leitura de contador (`LastNSLDate`)
  como proxy, com limite configurável em
  `PRINTER_OFFLINE_THRESHOLD_MINUTES`.

## Ajustando o intervalo de atualização

- Backend: `POLL_INTERVAL_SECONDS` no `.env` (frequência real de
  consulta à API do NDD).
- Frontend: `POLL_INTERVAL_MS` no topo do `<script>` em
  `static/index.html` (frequência que o navegador consulta o backend —
  pode ser mais curta que o backend sem problema, já que só lê do
  cache em memória, sem custo de chamar o NDD de novo).
