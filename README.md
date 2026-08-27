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
| `get_active_supplies()` | `OnDemandData.Execute` (`ActionName=RETRIEVEACTIVESUPPLIES`) |

## Limitações conhecidas da API (não são bugs deste código)

- **Nível percentual de toner** não é exposto por nenhum método
  documentado do NDD MPS.
- **Status online/offline** não existe como campo binário — este
  projeto usa a data da última leitura de contador (`LastNSLDate`)
  como proxy, com limite configurável em
  `PRINTER_OFFLINE_THRESHOLD_MINUTES`.

## Aba de Toner

O dashboard tem uma aba "Toner" (ao lado de "Impressoras", no modo completo)
com a mesma lista de impressoras — incluindo a barra lateral de instituições
e a coluna IP/Serial, do mesmo jeito que a aba Impressoras — mostrando o
nível de dois tipos suprimento por máquina: **Toner** e **Fotocondutor**
(cilindro). Os dados vêm do endpoint `OnDemandWS.OnDemandData` (`Execute`
com `ActionName=RETRIEVEACTIVESUPPLIES`), buscado **numa única chamada para
todas as instituições de uma vez** — diferente do `GetPrinters`, que
precisa ser chamado por instituição. Isso resolve a limitação antiga
descrita neste README (o candidato anterior, `GetPrintersSlots`, só
consulta uma impressora por vez e não seria viável pra ~3.500 máquinas
a cada ciclo).

Cada impressora pode ter vários slots de cada suprimento (ex: um por cor,
em modelos coloridos). O `main.py` classifica cada slot retornado numa
categoria, usando palavras-chave no `SupplySubFunctionTypeName` (ver
`_SUPPLY_CATEGORIES` em `main.py` — hoje reconhece "toner" e
"fotocondutor"/"drum"/"cilindro"/"photoconductor"), e para cada categoria
calcula:

- `<categoria>_level` — o **pior** percentual entre os slots daquele tipo
  na impressora (ex: `toner_level`, `fotocondutor_level` — é o número
  mostrado na coluna da tabela, colorido: vermelho ≤10%, amarelo ≤25%).
- `<categoria>_detail` — a lista completa por cor/slot, disponível no
  tooltip (passe o mouse sobre o percentual).

Adicionar um novo suprimento na tela (ex: fusor) é só acrescentar uma
linha em `_SUPPLY_CATEGORIES` no `main.py` e a coluna correspondente no
`index.html` — o resto (cruzamento com a impressora, cor de alerta,
tooltip) já é genérico.

Se uma impressora não tiver nenhum suprimento de um tipo retornado pela
API nesse ciclo, a coluna correspondente mostra "sem dado" em vez de
zero — evita confundir "sem informação" com "suprimento vazio". Se a
chamada à API falhar por completo num ciclo, o erro é logado e a coleta
segue normalmente (as impressoras simplesmente mantêm o último valor,
sem derrubar o resto do dashboard).

Essa aba também pode ser aberta isolada numa das 4 telas do escritório
via `?view=toner`.

## Modo "4 telas" (kiosk)

O mesmo `index.html` aceita um parâmetro `?view=` na URL pra mostrar só
uma parte do dashboard — útil quando cada tela do escritório fica fixa
numa página diferente. Sem `?view=` (ou `?view=all`), mostra tudo junto
como antes.

| URL | O que mostra |
|---|---|
| `http://SEU_SERVIDOR/` | Dashboard completo (padrão, sem mudanças) |
| `http://SEU_SERVIDOR/?view=overview` | Só os KPIs (online/offline/páginas/alertas/estoques), em fonte grande — pra ler de longe |
| `http://SEU_SERVIDOR/?view=events` | Só o feed de eventos/alertas, ocupando a tela inteira |
| `http://SEU_SERVIDOR/?view=rotate` | Tabela de impressoras, girando automaticamente entre instituições a cada 20s |
| `http://SEU_SERVIDOR/?view=rotate&interval=30` | Igual acima, mas trocando de instituição a cada 30s em vez de 20 |
| `http://SEU_SERVIDOR/?view=toner` | Só a tabela de nível de toner por impressora |

Sugestão de distribuição pras 4 telas:
1. `?view=overview` — visão geral rápida
2. `?view=events` — alertas abertos (a mais importante pra ação imediata)
3. `?view=rotate` — detalhe rotativo por instituição (TJ_PA, POLICIACIVIL, TCE_PA, BANPARÁ etc.)
4. Dashboard completo (`/`, sem parâmetro) ou outra rotação com `interval` diferente

## Ajustando o intervalo de atualização

- Backend: `POLL_INTERVAL_SECONDS` no `.env` (frequência real de
  consulta à API do NDD).
- Frontend: `POLL_INTERVAL_MS` no topo do `<script>` em
  `static/index.html` (frequência que o navegador consulta o backend —
  pode ser mais curta que o backend sem problema, já que só lê do
  cache em memória, sem custo de chamar o NDD de novo).
