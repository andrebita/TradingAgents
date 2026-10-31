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

3. **Nenhuma regra simples mostrou vantagem positiva robusta em 2026,** considerando a
   dependência entre células, a seleção dos papéis e os custos. Isso **não** é o mesmo
   que demonstrar equivalência ao acaso: no universo de 29 ações, momentum de 20 e
   60 dias e MM20 > MM50 tiveram intervalos inteiramente **negativos**, ou seja, o mercado
   de 2026 puniu as tendências de curto prazo. Inverter o sinal dessas regras não é uma
   vantagem demonstrada, porque o resultado veio de um único período escolhido depois
   de visto. A referência prática para o LLM continua sendo o controle aleatório
   (acerto ~50%, alfa ~0).

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
média de `direção × alfa`. "net" desconta 0,20% de custo de ida e volta por célula, uma
aproximação, não um retorno investível. Os intervalos de confiança (IC) usam bootstrap
**por blocos circulares de datas consecutivas** (6 semanas; 8 para o horizonte de 20
pregões), sobre a grade completa de datas. Isso preserva a correlação entre ações da
mesma semana e a sobreposição de janelas entre semanas vizinhas, que existe até com 5
pregões quando há feriado. O IC por célula (independente) aparece em
`results/baselines_console.txt` só para comparação: ele é estreito demais.

**Fora da amostra — 2026 (05/01 a 23/10, semanal), 5 ações do handoff, 210 células**

| estratégia | acerto | signed α | líquido | IC 95% (blocos de 6 datas) |
|---|---|---|---|---|
| comprar sempre | 48,1% | −0,07% | −0,27% | [−0,66%, +0,50%] |
| momentum 20d | 50,5% | +0,03% | −0,17% | [−0,34%, +0,46%] |
| momentum 60d | 42,9% | −0,34% | −0,54% | [−1,02%, +0,25%] |
| momentum 12-1 | 47,6% | −0,06% | −0,26% | [−0,66%, +0,54%] |
| reversão 5d | 41,4% | −0,25% | −0,45% | [−0,73%, +0,34%] |
| preço > MM50 | 45,7% | −0,12% | −0,32% | [−0,59%, +0,42%] |
| MM20 > MM50 | 36,7% | −0,36% | −0,56% | [−0,82%, +0,24%] |
| RSI14 reversão (28 células) | 53,6% | +0,34% | +0,14% | [−0,60%, +1,28%] |
| aleatório (1.000 sementes) | 50,0% (faixa 95%: 43,3–56,7%) | ~0 | | |

**Mesmo período e ações, horizonte de 20 pregões (195 células, IC por blocos de 8 datas):**
comprar sempre 52,3% e +0,43% [−1,30%, +2,14%]; MM20 > MM50 38,5% e −1,21%
[−2,36%, −0,17%]; aleatório com faixa de acerto de 43,1% a 56,9%. Com janelas de
20 pregões numa grade semanal, cada janela se sobrepõe às três seguintes. Por isso os
intervalos ficam bem mais largos.

**Fora da amostra — 2026, 29 ações líquidas do Ibovespa, 1.218 células**

| estratégia | acerto | signed α | IC 95% (blocos de 6 datas) |
|---|---|---|---|
| comprar sempre | 49,2% | −0,12% | [−0,28%, +0,05%] |
| momentum 20d | 48,6% | −0,27% | [−0,57%, −0,01%] |
| momentum 60d | 46,1% | −0,36% | [−0,61%, −0,05%] |
| MM20 > MM50 | 45,3% | −0,32% | [−0,57%, −0,06%] |
| aleatório | 50,0% (faixa 95%: 47,4–53,0%) | ~0 | |

Desenvolvimento, 2021–2025: com as 5 ações, "comprar sempre" acerta 53,3% (+0,20%);
com as 29, acerta 50,5% (+0,13%). Esses números estão **inflados por viés de
sobrevivência e de seleção**: as listas são de empresas que existem e são relevantes
hoje, e as 5 foram escolhidas olhando para trás. Não são uma referência válida.
Tabelas completas em `results/baselines_console.txt` e `results/summary_*.json`.
Cada célula está em `results/cells_*.csv`.

Universo amplo, problemas encontrados: `CPLE6.SA` não retornou dados no Yahoo e foi
excluído. `EMBR3.SA` não retornou dados, e foi usado `EMBJ3.SA`. Esses são exemplos
de mudança de ticker que um backtest de longo prazo precisa tratar.

## Revisão independente (GPT-6 Astra, commit `12b5e52`) e correções

Uma revisão externa encontrou problemas reais de vazamento e de medição. As correções
estão no commit `5196112` e nos seguintes. Situação de cada achado:

| # | Achado | Situação |
|---|---|---|
| F1 | CVM revelava o futuro: avisava ao agente que um balanço "foi reapresentado depois" e procurava o emissor no FCA do ano atual | **Corrigido.** O FCA só é lido até o ano de D e só quando entregue até D. O período cuja única versão guardada é posterior a D é omitido **sem aviso**. Resta o limite da fonte: a omissão em si depende de uma reentrega futura, porque a CVM não guarda versões antigas. |
| F2 | Focus ignorava feriados; o SGS não guarda versões originais | **Corrigido** o calendário: o corte segue o 1º dia útil da semana (feriados nacionais, Carnaval, Sexta-feira Santa e Corpus Christi). As séries revisadas (IBC-Br, desemprego) avisam ao agente que os valores são os revisados de hoje. Continua sem as versões originais. |
| F3 | Probabilidade e alfa distorcidos (0,58% lido como 58%, 50,4% arredondado para 50%, alfa arredondado no Brier) | **Corrigido.** Uma unidade só, sem arredondar a inclinação; o Brier e o acerto usam o alfa sem arredondamento; probabilidade ausente ou inválida conta como falha de medição. |
| F4 | O esforço não era enviado ao Haiku 5.5 | **Corrigido.** O manifesto registra o esforço efetivo de cada modelo. |
| F5 | Reaproveitar um `run_id` misturava configurações | **Corrigido.** Manifesto por `run_id`; uma retomada com qualquer diferença é recusada. A comparação recusa baselines de outro horizonte. |
| F6 | Inferência ignorava a dependência entre semanas; a conclusão do README era forte demais | **Corrigido.** Bootstrap por blocos de datas e texto revisto (conclusão 3). |
| F7 | A sonda de cobertura consultava indicadores errados | **Corrigido.** A sonda usa `selic`; o modelo roteirizado passa um indicador macro à ferramenta macro. |
| F8 | Preços ajustados por proventos posteriores a D | **Em aberto.** Os retornos não mudam, mas os níveis de preço citados pelos agentes (suportes, stops) podem não ser os negociados em D. Exige uma série de preços reconstruída para a decisão. |
| F9 | "Nenhum documento" podia ser falta de cobertura | **Corrigido.** Arquivo ausente ou cópia antiga aparecem como cobertura incompleta. O texto avisa que só vão tipo e assunto, não o conteúdo. |
| F10 | Seleção de período nos fluxos podia descartar contas | **Corrigido.** O período é escolhido por cobertura de contas; moeda e escala desconhecidas são rejeitadas; os agentes são avisados para não comparar colunas de durações diferentes. |
| F11 | O teto de gasto só era checado entre células; o workflow o ignorava | **Corrigido.** Checagem antes de cada chamada, acumulada entre retomadas; para se o uso de tokens não vier. O workflow usa o executor com teto e não esconde falhas. |
| F12 | A reflexão afirmava fatos sobre a trajetória de preços que não recebeu | **Corrigido** no prompt: a reflexão sabe que só tem os dois retornos. |

Ressalvas que permanecem:
- Um teste em datas passadas com um modelo atual é uma avaliação retrospectiva, não uma estratégia que poderia ter sido executada naquela época.
- O contexto do instrumento traz o nome atual da empresa.
- As datas 14/09 e 21/09 já foram usadas para decidir mudanças, então agora são desenvolvimento, não teste intocado.

## Segunda revisão independente (commit `04d6e00`) e correções

Correções no commit `4509969`.

| # | Achado | Situação |
|---|---|---|
| N1 | Uma parada fatal num worker não parava os outros; o teto não reservava as chamadas em andamento | **Corrigido.** Sinal de parada comum, checado antes de cada célula, e cancelamento das ações não iniciadas. Cada chamada reserva uma estimativa antes de sair; o gasto fica gravado chamada a chamada, e nenhuma chamada nova começa depois da parada. O excesso fica limitado às reservas em andamento; uma requisição já enviada ainda termina. |
| N2 | Com workers, as lições de memória dependiam da ordem de término | **Corrigido.** `use_decision_memory`: com a memória desligada (padrão no executor de experimentos), não há lições nem reflexões e nenhuma chamada ao modelo para elas. |
| N3 | A probabilidade podia ser a de outra parte citada no texto; a unidade era ambígua | **Corrigido.** A probabilidade fica gravada como número (`final_probability`) no estado e no log de estado. No texto livre, só conta uma ocorrência inequívoca. Unidade única: pontos percentuais. |
| N4 | O manifesto não garantia a mesma configuração e os mesmos dados | **Parcial.** Código não commitado é recusado; o manifesto inclui memória, workers, retries, limites de tokens e versões das dependências; cada invocação grava uma impressão dos arquivos de dados e avisa se mudou. Os dados não são congelados nem impostos, e o `run_backtest` nativo não tem manifesto. |
| N5 | O calendário do Focus não cobre interrupções extraordinárias; o FCA autorizava linhas por CNPJ | **FCA corrigido** (vale por documento: emissor, referência e versão). **Focus parcial:** feriados regulares corretos, mas paralisações como a greve do BCB de 2022 (edição de 08/04/2022 publicada só em 26/04) exigiriam uma tabela de publicação. Não usar datas de 2022 sem essa tabela. |
| N6 | O snapshot tratava uma lacuna do índice como dia parado; volume ausente quebrava o cálculo | **Corrigido.** Uma cotação do benchmark com mais de 5 dias de defasagem é tratada como lacuna; o volume médio só aparece com dados. O F8 continua aberto. |
| N7 | Uma decisão argumentou que o Ibovespa é índice de preço | **Corrigido.** O horizonte informado aos agentes descreve o contrato de avaliação: entrada, saída, retorno total por fechamentos ajustados e o Ibovespa como índice de retorno total. |
| N8 | As comparações gravadas usavam o leitor antigo | **Corrigido.** `compare_diag_*.json` e `diag_summary_table.json` foram regenerados com o avaliador `4509969`. Os Brier batem com o recálculo independente. |

Ressalvas que permanecem:
- O teste da saída estruturada nativa só confirma a escolha do método; a aceitação real pela API será verificada na próxima validação (chamadas ao gestor por célula e contador de fallbacks).
- O workflow do GitHub limita o gasto por execução do workflow, não acumulado entre execuções.

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

## Execuções com LLM até agora e próximos experimentos

Validações já feitas, todas com Claude Haiku 5.5 no nível rápido e Sonnet 5.5 no forte, 5
ações × 14 e 21/09/2026, horizonte de 5 pregões. Os resultados estão em
`results/llm_validation/`.

| run | analistas | dados | notas | US$/célula |
|---|---|---|---|---|
| `claude_validation` | market | só preços | 10 Hold | 0,087 |
| `claude_validation_br` | market, news | código antigo (sem CVM/BCB) | 10 Hold | 0,089 |
| `claude_validation_br2` | market, news | CVM IPE + BCB | 10 Hold | 0,097 |

Essas execuções provam que o pipeline funciona e que os dados chegam aos agentes. Elas
**não** medem qualidade, por três motivos: o esforço do Haiku não era aplicado (F4), as
decisões ainda não traziam probabilidade, e o horizonte não era informado aos agentes.

**Diagnóstico controlado** (`diag_A..D`, código `4112f83`, ITUB4 e PSSA3 × 14 e 21/09,
2 repetições, US$ 2,49). Brier recalculado com o avaliador `4509969`:

| Config. | Analistas | Horizonte | Esforço | Notas | Brier r1 / r2 |
|---|---|---|---|---|---|
| A | market, news | 5 | low | 3 Hold + 1 Underweight (nas duas) | 0,224 / 0,231 |
| B | market, news | 20 | low | 3 Hold + 1 Underweight (nas duas) | 0,234 / 0,229 |
| C | market, news | 20 | medium | 3H+1U / 2H+2U | 0,229 / 0,213 |
| D | + fundamentals | 20 | medium | 2 Hold + 2 Underweight (nas duas) | 0,213 / 0,213 |

O que dá para concluir: a execução funciona e é estável entre repetições (11 de 16 probabilidades
iguais, diferença média de 0,75 p.p.). O que não dá: calibração, vantagem ou eficiência de mercado.
São só 4 pares ação–data, sobrepostos em h20. Nenhuma probabilidade passou de 50%, e as decisões
apontaram a falta de dados do Ibovespa como lacuna. Essa lacuna foi corrigida depois, no snapshot
(`04d6e00`).

Próximos passos, seguindo a revisão:

1. **Validação funcional do código atual** (configuração D, 4 células, `--workers 2`). Critérios:
   uma chamada do gestor por célula e nenhum fallback de saída estruturada; probabilidade
   gravada como número nas 4 decisões; força relativa citada com janela e números corretos;
   custo e latência por chamada no `*_calls.jsonl`.
2. **Valor incremental da arquitetura** (15 ações × 4 janelas de 20 pregões sem
   sobreposição, depois do corte de treino, datas escolhidas sem olhar os retornos). Compara
   o sistema completo, um decisor único com o mesmo pacote de dados e os baselines.
   Métrica principal: diferença pareada de Brier. Teto de ~US$ 35. Isso não certifica
   uma vantagem de 3 a 5 p.p.; serve para descartar configurações ruins ou corrigir a medição.
   Desenho do decisor único, conforme a segunda revisão:
   - os dois braços recebem os mesmos relatórios dos analistas, congelados e identificados por hash;
   - o mesmo decisor final (Sonnet 5.5, esforço medium);
   - o mesmo prompt, schema e contrato de avaliação;
   - memória desligada nos dois braços;
   - a probabilidade é a saída principal.

   Isso mede o valor de todo o processamento depois dos analistas (debate, gestor de pesquisa,
   trader e risco), não só do debate. Com 4 janelas, a resposta é um piloto: o menor p-valor
   possível de um teste exato de sinais é 0,125.

Use sempre um `--run-id` novo por configuração (o manifesto recusa misturas) e baselines
gerados com o mesmo `--holding`. O workflow `.github/workflows/br-backtest.yml` (disparo
manual) roda o executor com teto, os baselines na mesma grade e a comparação.

## Custo estimado da execução com LLM

`estimate_tokens.py` roda o grafo real sobre dados reais e conta os tokens de cada
prompt com `o200k_base`, usando respostas de tamanho controlado. Medição com
`--analysts market` e respostas de ~1.200 tokens: 8 chamadas *quick* (30,5k tokens de
entrada / 8,6k de saída) e 2 chamadas *deep* (9,4k / 2,4k) por célula. Some 1 chamada de
reflexão na liquidação.

No cenário realista, os números medidos foram acrescidos de 30% (rodadas extras de
ferramenta) e de ~4k tokens de raciocínio por chamada *deep*. Preços usados:
`gpt-6-luna` US$ 0,10 / 0,50 e `gpt-6-sol` US$ 2 / 10 por milhão de tokens. São preços
de fontes secundárias (out/2026); confira na página da OpenAI. Câmbio PTAX de 30/10/2026: R$ 5,42.

| cenário | por célula | 55 células | 800 células |
|---|---|---|---|
| otimista (padrão luna+sol) | US$ 0,02 | US$ 1,3 (R$ 7) | US$ 19 (R$ 100) |
| realista (padrão luna+sol) | US$ 0,14 | US$ 7,6 (R$ 41) | US$ 110 (R$ 600) |
| pessimista (padrão luna+sol) | US$ 0,28 | US$ 15 (R$ 82) | US$ 220 (R$ 1.200) |
| realista, tudo em sol | US$ 0,33 | US$ 18 (R$ 97) | US$ 260 (R$ 1.410) |

Com o plano ChatGPT não há cobrança extra, mas cada célula faz ~12–15 chamadas, que
contam no limite de uso do plano: cerca de 700–800 chamadas para 55 células e
10–12 mil para 800 células.

## Escolha de modelo (pesquisa de 30/10/2026)

O critério principal é diferente para cada uso:

- **Backtest histórico:** o que importa é a data de corte do treino. Um período antes
  do corte não é teste limpo, porque o modelo pode lembrar os preços e as notícias
  (Look-Ahead-Bench, arXiv 2601.13770; FinCAD, arXiv 2605.24564). Os dois níveis
  (quick e deep) precisam ter corte anterior ao período testado. Deixe ~1 mês de folga.
- **Uso diário e teste prospectivo:** o corte não importa. O que importa é a
  qualidade da análise por real gasto.

| modelo | Finance Agent v2.1 (Vals, 29/10/2026) | corte (fontes secundárias) | US$/M entrada/saída |
|---|---|---|---|
| Claude Opus 5.5 | 67,85% (1º) | jun/2026 | 4 / 20 |
| Gemini 4 Argon | 67,66% | ? | 2/10 até 30/11, depois 4/20 |
| Claude Sonnet 5.5 | 67,28% | jun/2026 | 2 / 10 |
| Gemini 3.8 Flash | 65,76% | mar/2026 | 0,75 / 3,75 até 31/12 |
| Claude Haiku 5.5 | 65,37% | jun/2026 | 0,10 / 0,50 |
| GPT-6.1 Sol | 61,82% | ~abr/2026 (6 Sol) | 2 / 10 (6 Sol) |
| GPT-6 Luna | 56,84% | mai/2026 | 0,10 / 0,50 |
| Gemini 3.1 Pro / 3.1 Flash Lite | — | jan/2025 | 2 / 12 e 0,25 / 1,50 |

O Finance Agent mede pesquisa em documentos financeiros, não previsão de retorno.
Nenhum benchmark mostra que um LLM gera alfa.

Recomendação:

1. **Validação da infraestrutura (~50 células):** plano ChatGPT (`chatgpt`), sem custo extra.
2. **Backtest retroativo com amostra suficiente:** `gemini-3.1-pro-preview` (deep) +
   `gemini-3.1-flash-lite` (quick), `google_thinking_level=medium`. É a única combinação
   com corte (jan/2025) que deixa ~20 meses limpos (mar/2025 a out/2026). São ~2.500
   células possíveis em 29 ações, ~US$ 0,18 por célula.
3. **Uso diário + teste prospectivo semanal:** `claude-haiku-5-5` (quick) +
   `claude-sonnet-5-5` (deep), `anthropic_effort=high`. O repositório não envia
   `effort` para Haiku, que fica no padrão `medium`. Custa ~US$ 0,14 por célula, o mesmo
   que o padrão OpenAI, com nota de finanças bem maior. São ~R$ 95/mês para 29 ações por semana.

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
