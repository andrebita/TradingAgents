# Experimento 0 — Referência quantitativa para ações da B3

Data de execução: 30/10/2026. Base: `TauricResearch/TradingAgents` v0.7.1, commit
`c454491` (o `main` deste fork é idêntico ao `main` do upstream nessa data).
Licença do upstream: Apache 2.0. Redistribuir um fork é permitido, desde que se
mantenham a licença e os avisos e que os arquivos alterados sejam marcados como tal.

Este diretório contém o primeiro experimento executado do projeto TradingAgents
Brasil. **Nenhum modelo de linguagem foi chamado.** O ambiente não tinha
credencial de API de LLM, e contratar uma não estava autorizado. O que foi feito:

1. Mapear quais dados chegam de fato aos agentes para ações `.SA` em datas passadas.
2. Rodar o pipeline nativo `run_backtest` / `settle_pending` / `summarize` de ponta
   a ponta, em 5 ações × 42 datas, usando um modelo roteirizado no lugar do LLM.
3. Calcular baselines simples (comprar sempre, momentum, médias móveis, RSI,
   sinal aleatório) com a mesma métrica do backtest nativo, para estabelecer a
   referência que uma execução com LLM precisa superar.
4. Deixar pronta a execução com LLM (workflow do GitHub Actions + script de comparação).

## Conclusões principais

1. **Em backtest histórico, o TradingAgents v0.7.1 só vê preços para ações da B3.**
   Nas 210 células (5 ações × 42 segundas-feiras de 2026), as únicas ferramentas
   com dados foram `get_stock_data`, `get_indicators` e `get_verified_market_snapshot`.
   As demais vieram vazias ou bloqueadas, em 210 de 210 células:
   - fundamentos, balanço, DRE, fluxo de caixa e insiders: *withheld*. O Yahoo não informa
     a data de publicação, e o SEC EDGAR só cobre emissores americanos;
   - notícias da empresa: `DATA_UNAVAILABLE` (o Yahoo não tem notícias com a tag `ITUB4.SA` etc.);
   - notícias globais: indisponíveis para datas passadas (o Yahoo só serve itens recentes);
   - macro: `DATA_UNAVAILABLE` (só FRED, que exige `FRED_API_KEY`; não há BCB);
   - mercados de previsão: *withheld* (o Polymarket só tem odds ao vivo);
   - sentimento: StockTwits responde 404 para `.SA`, e o Reddit limitou as requisições (HTTP 429).

   Consequência: rodar o TradingAgents "completo" em datas passadas da B3 equivale,
   em termos de informação, a rodar só o analista técnico, mas com mais custo.
   Os protetores contra look-ahead funcionam (nada vazou). O problema é que, para o
   Brasil, eles bloqueiam quase tudo. Isso confirma a Issue #1522 e justifica os
   conectores CVM/BCB.

2. **A infraestrutura nativa funciona para `.SA`.** 210 células rodadas, 210 liquidadas
   contra `^BVSP`, nenhuma falha. O alfa que o framework registrou bate com um
   recálculo independente em todas as células (diferença máxima de 0,05 p.p., que é o
   arredondamento do log).

3. **Nenhuma regra simples se distingue do acaso em 2026.** Isso vale tanto para as 5
   ações quanto para um universo de 29 ações líquidas. A referência que um LLM
   precisa superar é, na prática, a de um sinal aleatório: taxa de acerto de 50% e alfa ~0.

4. **A grade sugerida no handoff não tem poder estatístico.** `--every 28` em 5 ações
   dá cerca de 50 células. Com 50 células, um sinal aleatório já fica entre ~36% e ~64%
   de acerto. Para distinguir 55% de acerto de 50% (α=5%, poder 80%) são necessárias
   ~780 células independentes. Para 53%, ~2.200. Ver "Desenho para a execução com LLM".

## Resultados

### Backtest nativo com "comprar sempre" (sem LLM)

`results/native_always_buy_2026/` — o mesmo `run_backtest` do comando
`tradingagents backtest`, com os analistas `market,news,fundamentals` e 5 pregões de
holding contra o `^BVSP`.

```
Resolved cells: 210 · pending: 0
- Buy: n=210, called the direction 47%, mean alpha -0.07% vs the benchmark
```

Chamadas ao modelo: 3.150, ou seja, 15 por célula com 3 analistas e o modelo
roteirizado (uma rodada de ferramentas). Um LLM real faz pelo menos isso,
e em geral mais.

### Baselines (regras, sem LLM)

Métrica: para cada célula (ação, data), o alfa é o retorno da ação menos o do
Ibovespa, da 1ª cotação de fechamento após a data até 5 pregões depois. É a mesma
definição de `fetch_returns`, com preços ajustados por proventos. "signed α" é a
média de `direção × alfa`. "net" desconta 0,20% de custo de ida e volta por célula.
Os intervalos de confiança (IC) foram obtidos por bootstrap reamostrando células e,
mais honesto, reamostrando datas (células da mesma semana são correlacionadas).

**Fora da amostra — 2026 (05/01 a 23/10, semanal), 5 ações do handoff, 210 células**

| estratégia | acerto | signed α | líquido | IC 95% (por data) |
|---|---|---|---|---|
| comprar sempre | 48,1% | −0,07% | −0,27% | [−0,65%, +0,62%] |
| momentum 20d | 50,5% | +0,03% | −0,17% | [−0,57%, +0,66%] |
| momentum 60d | 42,9% | −0,34% | −0,54% | [−0,93%, +0,17%] |
| momentum 12-1 | 47,6% | −0,06% | −0,26% | [−0,66%, +0,60%] |
| reversão 5d | 41,4% | −0,25% | −0,45% | [−0,82%, +0,41%] |
| preço > MM50 | 45,7% | −0,12% | −0,32% | [−0,73%, +0,52%] |
| MM20 > MM50 | 36,7% | −0,36% | −0,56% | [−0,91%, +0,30%] |
| RSI14 reversão (28 células) | 53,6% | +0,34% | +0,14% | [−0,76%, +1,35%] |
| aleatório (1.000 sementes) | 50,0% (faixa 95%: 42,9–56,7%) | ~0 | | |

**Fora da amostra — 2026, 29 ações líquidas do Ibovespa, 1.218 células**

| estratégia | acerto | signed α | IC 95% (por data) |
|---|---|---|---|
| comprar sempre | 49,2% | −0,12% | [−0,28%, +0,06%] |
| momentum 20d | 48,6% | −0,27% | [−0,71%, +0,10%] |
| momentum 60d | 46,1% | −0,36% | [−0,68%, −0,03%] |
| MM20 > MM50 | 45,3% | −0,32% | [−0,64%, −0,04%] |
| aleatório | 50,0% (faixa 95%: 47,3–52,7%) | ~0 | |

Desenvolvimento, 2021–2025: com as 5 ações, "comprar sempre" acerta 53,3% (+0,20%);
com as 29, acerta 50,5% (+0,13%). Esses números estão **inflados por viés de
sobrevivência e de seleção**: as listas são de empresas que existem e são relevantes
hoje, e as 5 foram escolhidas olhando para trás. Não são uma referência válida.
Tabelas completas em `results/baselines_console.txt` e `results/summary_*.json`.
Cada célula está em `results/cells_*.csv`.

Universo amplo, problemas encontrados: `CPLE6.SA` não retornou dados no Yahoo e foi
excluído. `EMBR3.SA` não retornou dados, e foi usado `EMBJ3.SA`. Esses são exemplos
de mudança de ticker que um backtest de longo prazo precisa tratar.

## Auditoria de `tradingagents/backtest.py` e da liquidação

- **Entrada e saída:** entra no 1º fechamento *após* a data de análise e sai
  `holding_period_days` (5) pregões depois. O benchmark é avaliado nas mesmas datas.
  Usa `history()` do yfinance, que ajusta por proventos, e o `^BVSP` é índice de
  retorno total. As duas pontas são consistentes.
- **Ponto no tempo:** as ferramentas limitam as datas à data de análise (`as_of`,
  `as_of_window`). As lições da memória só entram se já estavam liquidadas na data da
  análise (`_memory_as_of`). Nenhum vazamento foi encontrado no caminho de dados.
- **Viés de arredondamento:** o log guarda o alfa com 0,1 p.p. de precisão, e
  `summarize` conta como erro uma célula com alfa `0.0%`. Nesta execução, 3 de 210
  células foram afetadas: 46,7% nativo contra 48,1% sem arredondamento. É um viés
  pequeno, sempre contra o sistema.
- **Hold não é pontuado.** Um modelo que responde muito Hold reduz a amostra efetiva.
- **Não é simulador de carteira** (está documentado no próprio arquivo). Não modela
  custos, tamanho de posição, patrimônio nem drawdown. Os baselines acima reportam
  um valor líquido de 0,20% apenas como indicação.
- **Normalização de ticker:** `normalize_symbol("PETR4")` devolve `PETR4`, sem `.SA`.
  É preciso sempre passar o sufixo.
- **Look-ahead pelo próprio LLM (risco não coberto pelo código):** um modelo cujo
  treino vai além da data de análise pode "lembrar" o que aconteceu depois. Por
  isso, períodos anteriores ao corte de conhecimento do modelo (por exemplo,
  2021–2025 para modelos atuais) não servem como teste limpo do LLM. O teste fora da
  amostra precisa ficar **depois** do corte de treino do modelo usado. A divisão do
  handoff (desenvolver em 2021–2025, testar em 2026) só é válida se o modelo tiver
  corte anterior a 2026. Registrar o corte de cada modelo usado.

## PR #705 (B3) — avaliação

O `b3.py` do PR é só uma camada sobre o yfinance que acrescenta `.SA` a tickers no
formato `^[A-Z]{4}[0-9]{1,2}F?$`. Ele **não traz nenhuma fonte de dados nova**:
nada de CVM, BCB ou notícias brasileiras. Além disso, depende de módulos que não
existem mais na v0.7.1 (`dataflows/interface.py`, `y_finance.py`) e inclui relatórios
gerados (`reports/BBAS3.SA_*`) no próprio diff. O único item aproveitável é a regex
de normalização de ticker. Não há motivo para incorporá-lo.

## Fontes brasileiras — acessibilidade verificada

- **CVM IPE** (`dados.cvm.gov.br/.../IPE/DADOS/ipe_cia_aberta_2026.zip`, 1,8 MB):
  37.861 documentos em 2026, com `Data_Entrega`, `Categoria`, `Assunto`, `Versao` e
  `Link_Download`. Itaú: 143 documentos, entre eles 6 Fatos Relevantes (por exemplo,
  04/08/2026 "Projeções 2026"). Serve como notícia corporativa ponto-no-tempo, como
  propõe a Issue #1522.
- **CVM DFP/ITR:** os zips anuais estão acessíveis (DFP 2025 com 12,8 MB, ITR 2026 com
  19,9 MB). Contêm `DT_RECEB` (data de entrega) e `VERSAO`, o que permite montar
  demonstrativos ponto-no-tempo, incluindo reapresentações.
- **BCB SGS:** a API responde (por exemplo, série 433, IPCA mensal).

## Desenho para a execução com LLM (próximo passo — exige decisão do usuário)

O usuário precisa definir: provedor e modelo (com o corte de treino conhecido),
orçamento e, então, cadastrar a chave como *secret* do repositório.

Recomendação:
- **Etapa 1 (infraestrutura com LLM, barata):** `--analysts market`, 5 ações,
  `--every 28`. Cerca de 50 células. Serve **só** para validar a execução e medir
  custo por célula, não para medir qualidade.
- **Etapa 2 (medição):** ≥ 800 células fora do corte de treino. Por exemplo, as 29
  ações de `broad29` × 28 datas semanais. Pode ser em 2026 se o modelo tiver corte
  anterior a 2026. Comparar com `compare_run.py`, que põe o LLM ao lado de todos os
  baselines **nas mesmas células** e da faixa do aleatório.
- Repetir pelo menos uma vez com outra semente/temperatura para medir a variabilidade.

O workflow `.github/workflows/br-backtest.yml` (disparo manual) faz exatamente isto:
roda `tradingagents backtest`, roda os baselines na mesma grade, compara e publica
tudo como artefato.

## Reprodução

```bash
pip install -e ".[dev]"
T5=ITUB4.SA,PSSA3.SA,SBSP3.SA,BBDC4.SA,B3SA3.SA

# 1. o que chega aos agentes numa data passada (sem LLM)
python experiments/br_baseline/probe_data_coverage.py experiments/br_baseline/results 2026-06-01

# 2. pipeline nativo ponta a ponta com modelo roteirizado ("comprar sempre")
python experiments/br_baseline/native_dryrun.py --out /tmp/native --tickers $T5 \
  --start 2026-01-05 --end 2026-10-23 --every 7 --rating Buy \
  --analysts market,news,fundamentals --run-id always_buy_2026

# 3. baselines
python experiments/br_baseline/baselines.py --out experiments/br_baseline/results \
  --tickers $T5 --start 2026-01-05 --end 2026-10-23 --every 7 --label five_oos_2026

# 4. comparar qualquer execução (por exemplo, com LLM) com os baselines nas mesmas células
python experiments/br_baseline/compare_run.py <results_dir>/backtest/<run_id>/trading_memory.md \
  experiments/br_baseline/results/cells_five_oos_2026.csv
```

Os dados vêm do Yahoo Finance no momento da execução. Ajustes de proventos e
correções posteriores podem mudar os números na segunda casa decimal.
