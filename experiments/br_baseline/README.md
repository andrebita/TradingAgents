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

## Terceira revisão independente: arquitetura do comitê (commit `32ea4a6`) e correções

O veredito da revisão foi bloquear recomendações de movimentação até corrigir C1, C2, A3 e C10. As correções
estão nos commits `4818fff`, `cf2e889`, `d44058e` e `217ffda`.

| # | Achado | Situação |
|---|---|---|
| A1 | Falta uma política por metas e prazos | **Aberto.** Depende das respostas do investidor (perguntas abaixo). |
| A2 | A estratégia executada não é a testada: a seleção era recalculada todo mês | **Corrigido.** A seleção fica congelada entre rebalanceamentos. O replay da rotina real mede o que ela entrega com atritos: 0,9 ponto ao ano acima do DIVO11, abaixo do mínimo pré-registrado de 1 ponto. O DIVO11 passa a ser o veículo padrão recomendado (seção "Replay da rotina real"). |
| A3 | Setores excluídos não eram aplicados; o dinheiro do mês não tinha teto | **Corrigido.** As exclusões saem da seleção, e o comitê as repassa. Aporte mais transferência nunca levam as ações acima do alvo mais a faixa. |
| A4 | A sombra ignora a alocação e a renda fixa | **Aberto.** A sombra mede só a parte em ações. |
| B1 | A variante semestral (13,5%) foi adotada depois de vista; a primária era 11,2% | **Respondido pelo replay.** Na rotina real as duas ficam praticamente iguais (TIR de 12,0% e 12,1% na escala B). O `CARTEIRA.md` mostra as duas. |
| B2 | A limpeza de preços inventava altas: [100, 5, 9,5] virava [100, 100, 190] | **Corrigido.** Só saltos que se desfazem em até 10 pregões são zerados. Um salto que fica é mantido e marcado, e a ação fica fora de compras novas por um ano. Se já estiver em carteira, não é vendida nem comprada. |
| B3 | Estresse por índices; tolerância de 20% fixa no código | **Parcial.** A tolerância virou campo da política e é comparada com a pior queda do mix-alvo. O estresse por posição fica para depois. |
| C1 | As ações de renda fixa do diretor não eram checadas | **Corrigido.** A checagem trata o plano como um balanço de dinheiro, com testes de resgate em dobro, título inexistente, título sem liquidez, aplicação infinita, emissor fora do FGC e reserva. |
| C2 | Reserva descoberta ainda mandava dinheiro para ações; o IPCA+ longo contava como reserva; o caixa não contava | **Corrigido.** A reserva conta caixa e pós-fixados de liquidez diária. O aporte a completa primeiro, e nada é transferido enquanto ela estiver incompleta. |
| C3 | O preço médio vinha do primeiro lote | **Corrigido.** Média ponderada entre corretoras; com um lote sem preço, o ganho fica desconhecido. |
| C4 | O IR estimado não era reservado | **Corrigido.** O IR fica no caixa e não é gasto em compras. |
| C5 | O histórico de IR não chegava ao comitê | **Corrigido.** O bloco `tax` do patrimônio leva vendas e ganho do mês e prejuízo acumulado à rotina e às ordens finais. |
| C6 | Vendas adiadas nunca eram retomadas | **Corrigido.** `vendas_pendentes.json`, retomado todo mês dentro da isenção. |
| C7 | ETF com valor era ignorado; preço ausente valia zero | **Corrigido.** Todas as classes são valoradas. Uma posição sem valor bloqueia a transferência e aparece na carta. |
| C8 | As threads perdiam o `run_config` | **Corrigido.** Cada analista roda numa cópia do contexto da reunião. |
| C9 | O orçamento só contava depois da chamada | **Corrigido.** `Budget` reserva o pior caso antes de cada chamada, é seguro entre threads e tem sub-orçamento para os alertas da rotina. |
| C10 | A carta mostrava números sem a checagem ("R$ 60 mil reservados" com patrimônio vazio) | **Corrigido.** A carta abre com o plano conferido: dados, saldos depois do plano, movimentações de/para, IR, pendências e aprovação. |
| C11 | Injeção de prompt chegava ao diretor | **Mitigado.** Textos externos vão delimitados como dados, e o poder do diretor é limitado pela checagem. O risco não some por completo. |
| C12 | Falha de saída estruturada do diretor abortava a reunião | **Corrigido.** Vale a decisão da regra, com aviso na carta. |
| C13 | O planejador genérico vendia FII com IR zero | **Corrigido.** A rotina cuida só de ações de empresas listadas; o resto aparece como fora dela. |
| D1 | O prompt do diretor leva o patrimônio inteiro à API | **Aberto.** Depende de o investidor dizer o que pode ir para a API. |
| F1 | Fable no diretor em vez de Opus não está justificado | **Aberto.** Pede uma comparação cega, paga. |

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

## Notícias brasileiras: texto dos documentos da CVM e imprensa (31/10/2026)

O vendor padrão de notícias para ações da B3 passou a ser `b3`, que junta duas fontes.
Todas as medições abaixo foram feitas ao vivo.

**Documentos entregues à CVM (`cvm`).** Depois da lista, entra o texto completo de Fatos
Relevantes, Avisos aos Acionistas e Comunicados ao Mercado, extraído do PDF de cada documento
no servidor da CVM (RAD).
- **Versão:** é a mesma da lista, ou seja, a que estava pública na data da entrega.
- **Download:** o servidor derruba cerca de 1/3 das conexões na primeira tentativa. Com até 4
  tentativas, foram 33 de 33 PDFs. Cada texto fica em cache para sempre.
- **Tamanho e limites:** em 36 documentos de 2026, a mediana é de cerca de 3 mil caracteres
  por fato relevante (10 de 12 têm menos de 6 mil) e de 2 a 20 mil por comunicado. Em
  português, ~2,4 caracteres = 1 token. Por isso há um teto de 15 mil caracteres por documento
  e 40 mil por chamada (`cvm_filing_text_chars`). O teto impede que um relatório de produção
  (38 mil caracteres) ou um release de resultado (41 mil) tire os outros documentos.
- **Excluídos:** os Dados Econômico-Financeiros (demonstrações e releases têm de 140 a 340 mil
  caracteres, até ~150 mil tokens), cujos números já vêm das ferramentas de demonstrativos.
- **Limitação:** tabelas coladas como imagem se perdem. Na projeção 2026 do Itaú, os números
  estavam numa imagem.

**Imprensa (`br_press`).** A API pública WordPress de InfoMoney, Money Times, Seu Dinheiro,
BP Money e NeoFeed, sem chave. Ela busca no arquivo por período e devolve texto completo,
`date_gmt` (publicação) e `modified_gmt` (última edição).
- **Alcance:** chega a 2025. Petrobras de 26 a 28/02/2025 trouxe 33, 56, 13 e 11 matérias.
- **Regra de data:** a matéria entra só se foi publicada na janela e **editada pela última
  vez até o fim dela**. Entre 0 e 36% das matérias por portal são editadas mais de um dia
  depois, e o texto atual poderia trazer fatos posteriores.
- **Ranking:** primeiro as matérias com manchete só sobre a empresa, depois as que a citam com
  outras, depois as que só a mencionam. Resumos com várias empresas ficam só com os parágrafos
  sobre ela.
- **Volume:** 30 mil caracteres de texto por chamada (`br_press_text_chars`). As respostas
  ficam em cache, então repetir uma janela passada não faz nova consulta.

**Volume por chamada.** PETR4, ITUB4 e VALE3, em semanas de resultado, dão ~31 mil tokens por
chamada: 4 a 5 documentos da CVM e 6 a 12 matérias. No Haiku 5.5, isso custa ~US$ 0,01 a
0,02 a mais por célula, contra ~US$ 0,17 da célula do grafo completo.

**Limites conhecidos**
- A imprensa é uma amostra de 5 sites gratuitos. Valor, Estadão e Folha ficam de fora por
  paywall ou falta de API.
- Matérias apagadas não aparecem.
- A busca é por código de negociação (PETR4), não pelo nome da empresa.

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

## Experimento 2: pré-registro (31/10/2026, antes de qualquer resultado)

Pergunta: o processamento depois dos analistas (debate otimista/pessimista, gestor de pesquisa,
trader e equipe de risco) melhora a previsão em relação a um decisor único com as mesmas evidências?

- **Código:** `experiment2.py` executa e `experiment2_eval.py` avalia.
  - O grafo completo roda uma vez por célula, com a memória desligada.
  - Os relatórios dos analistas, o snapshot determinístico e o contrato de avaliação viram um
    pacote congelado, gravado em arquivo com SHA-256.
  - O mesmo decisor final (Sonnet 5.5, esforço medium, mesmo prompt e mesmo schema JSON, sem
    fallback em texto livre) é chamado duas vezes: `full`, com pacote + deliberação, e `single`,
    só com o pacote.
  - A decisão original do gestor fica como referência (`original`).
  - Falhas contam como falha de medição.
- **Universo (15):** as 15 primeiras ações da lista `broad29` (definida em 30/10, antes de qualquer
  execução com LLM), sem ITUB4 e PSSA3, usadas no desenvolvimento: PETR4, VALE3, BBDC4, BBAS3, B3SA3,
  ABEV3, WEGE3, RENT3, SUZB3, PRIO3, EQTL3, RADL3, GGBR4, LREN3, SBSP3. A lista é de empresas
  relevantes hoje, então há viés de sobrevivência.
- **Datas (4 janelas de 20 pregões, sem sobreposição, todas liquidadas):**

  | Análise | Entrada | Saída |
  |---|---|---|
  | 06/07 | 07/07 | 04/08 |
  | 03/08 | 04/08 | 01/09 |
  | 31/08 | 01/09 | 30/09 |
  | 29/09 | 30/09 | 29/10 |

  Todas são posteriores ao corte de treino declarado dos modelos (jun/2026). A primeira fica a um mês dele.
- **Métrica primária:** diferença pareada de Brier, `(p_full − y)² − (p_single − y)²`, com média por
  janela e depois entre janelas; intervalo t com 3 graus de liberdade e teste exato de troca de
  sinais (p mínimo de 0,125).
- **Métricas secundárias:** Brier de cada braço contra 50%, cobertura, correlação de ranking por data,
  acerto do lado, custo por braço e variabilidade nas repetições (`--repeat 6`).
- **Como ler o resultado:** com 4 janelas é um piloto. Ele permite abandonar uma configuração, corrigir
  a medição ou decidir ampliar; não permite afirmar superioridade.

## Experimento 2: resultado (`exp2_pilot_full`, 60 células, 4 janelas, US$ 10,49)

Avaliação com `experiment2_eval.py` (`results/experiment2/exp2_eval.json`). As 60 células têm
desfecho; 51,7% delas bateram o Ibovespa em 20 pregões.

| Braço | Brier | Prob. média | Acima de 50% | Abaixo de 50% | IC de ranking (média das datas) |
|---|---|---|---|---|---|
| completo (pacote + deliberação) | 0,2528 | 46,1% | 0% | 87% | +0,08 |
| único (só o pacote) | 0,2506 | 47,5% | 8% | 78% | +0,09 |
| original (gestor do grafo) | 0,2578 | 44,6% | 2% | 85% | −0,02 |
| sempre 50% | 0,2500 | 50% | — | — | — |

- **Completo menos único:** +0,0022 de Brier por célula. Médias por janela: +0,0031, −0,0015, +0,0008 e
  +0,0065. IC95 entre janelas de −0,0032 a +0,0076; p de troca de sinais 0,375. Não há diferença detectável.
- **Calibração:** os três braços são sistematicamente pessimistas (média de 45% a 47,5% contra 51,7% de
  acerto real). Mesmo centrados na taxa real (ajuste feito dentro da própria amostra, só para ilustrar), o
  Brier fica em ~0,249: não há discriminação além da taxa de base.
- **IC de ranking por data** (completo): +0,12, +0,54, −0,50 e +0,17. É ruído com 15 ações por data.
- **Variabilidade:** as 15 células de 06/07 rodaram duas vezes do zero. Diferença média de 1,6 p.p. no
  completo, 1,1 no único e 2,3 no gestor original. Repetindo só o decisor sobre o mesmo pacote, 0,6 p.p.
- **Custo:** analistas US$ 0,90; meio do grafo (debate, plano, trader, risco, gestor) US$ 4,05;
  decisor completo US$ 3,55; decisor único US$ 1,99. O braço completo custa cerca de 3 vezes o único
  (~US$ 8,50 contra ~US$ 2,90) sem ganho mensurável.
- **Baselines nas mesmas células:** todos dentro da faixa do aleatório (36,7% a 61,7% de acerto).

Leitura: neste piloto, nem o sistema multiagente completo nem um decisor único com as mesmas evidências
mostram capacidade de prever o retorno relativo ao Ibovespa em 20 pregões. O processamento depois dos
analistas não melhora a previsão e triplica o custo. Com 4 janelas, isso não exclui uma vantagem pequena,
mas uma vantagem grande seria visível.

## Experimento 3: pré-registro (31/10/2026, antes de qualquer resultado)

**Pergunta.** O relatório de notícias melhora a previsão quando o analista lê o conteúdo de verdade, ou
seja, o texto dos fatos relevantes e avisos da CVM e a imprensa brasileira? No experimento 2 ele via só os
títulos dos documentos da CVM. Os relatórios antigos dizem literalmente "I could not see the content".

**Desenho** (`experiment3.py`). Parte dos 60 pacotes congelados do experimento 2. Cada pacote é conferido
pelo SHA-256 antes de ser usado.
- **O que muda:** só o relatório de notícias (E2). O analista de notícias roda sozinho, como no grafo
  (Haiku 5.5, esforço médio), com o vendor `b3`.
- **O que fica igual, byte a byte:** mercado (E1), fundamentos (E3), snapshot (E4) e contrato.
- **Decisor:** o do braço único do experimento 2 (Sonnet 5.5, esforço médio, mesmo prompt e esquema, sem
  deliberação). Responde a dois braços: `new` (pacote com o E2 refeito) e `old` (pacote original, perguntado
  de novo no mesmo dia e código).
- **Auditoria:** o que o analista leu (cada chamada de ferramenta e sua saída) fica em
  `*_news_inputs/`, para checar o filtro de data célula a célula.

**Métricas.**
- **Primária:** diferença de Brier pareada `new − old` por célula, com média por janela e depois entre as 4
  janelas. IC t entre janelas e teste exato de troca de sinais; com 4 janelas, o menor p possível é 0,125.
- **Secundárias:**
  - probabilidade média, para ver se o pessimismo de 45% a 47% persiste;
  - IC de ranking por data;
  - deslocamento médio |new − old| comparado ao ruído do decisor |old − exp2|, que é a mesma pergunta feita
    duas vezes;
  - cobertura: células em que o analista recebeu texto da CVM e da imprensa.

**O que o resultado pode e não pode dizer.**
- Com 60 células e 4 janelas, só um efeito grande é detectável. No experimento 2, o IC da diferença entre
  braços teve meia-largura de ~0,005 de Brier.
- Um resultado nulo não prova que notícias não ajudam. Um Brier claramente abaixo de 0,25 nas 4 janelas
  seria o primeiro sinal de discriminação.
- **Limitação:** o modelo pode ter visto, no treino, notícias de jul–out/2026 sobre essas empresas; isso vale
  igualmente para os dois braços.

**Custo.** Teste de fumaça (1 célula): US$ 0,065, sendo US$ 0,004 do analista e US$ 0,03 por decisão.
Previsão de ~US$ 3,90 para as 60 células, com teto de US$ 5.

    python experiments/br_baseline/experiment3.py --run-id exp3_news --workers 3 --budget-usd 5
    python experiments/br_baseline/experiment3_eval.py experiments/br_baseline/results/experiment3 exp3_news \
        --out experiments/br_baseline/results/experiment3/exp3_eval.json

## Experimento 3: resultado (`exp3_news`, 60 células, US$ 4,10)

A execução levou 21 minutos, sem falhas (`results/experiment3/exp3_eval.json`).

**Cobertura e auditoria.**
- O analista chamou `get_news` nas 60 células e recebeu texto de imprensa em todas, e texto de documentos da
  CVM em 35. A mediana foi de 36 mil caracteres lidos por célula.
- **Auditoria de data:** dos 2.294 itens datados que ele leu, nenhum é posterior à data analisada.

| Braço | Brier | Prob. média | Acima de 50% | IC de ranking (média das datas) |
|---|---|---|---|---|
| `new` (notícias reais) | 0,2525 | 48,2% | 23% | −0,06 |
| `old` (pacote do exp. 2, perguntado de novo) | 0,2520 | 47,5% | 8% | +0,06 |
| `exp2` (resposta original do exp. 2) | 0,2506 | 47,5% | 8% | +0,09 |
| sempre 50% | 0,2500 | 50% | — | — |

- **Primária, `new − old`:** +0,0005 de Brier. Por janela: +0,0076, −0,0038, −0,0015 e −0,0001.
  IC95 de −0,0073 a +0,0084; p de troca de sinais 1,0. Nenhum efeito.
- **O relatório mudou muito, a probabilidade quase nada.** O deslocamento médio foi de 1,4 p.p., contra
  0,4 p.p. de ruído do decisor (`old` contra `exp2`). Só 1 célula mudou mais de 5 p.p. As probabilidades
  ficam entre 44% e 53%, com desvio-padrão de 2,6 p.p.
- **Os deslocamentos não apontam para o desfecho.**
  - A correlação de ranking entre o deslocamento e o alfa foi +0,03.
  - Das 31 células que subiram, 52% bateram o Ibovespa; das 13 que desceram, 54%.
- **O pessimismo diminuiu um pouco:** a média passou de 47,5% para 48,2%, e a parcela acima de 50% de 8%
  para 23%. Isso não melhorou o Brier.

Leitura: dar ao analista o conteúdo real (texto dos fatos relevantes e imprensa com visões de corretoras)
torna o relatório muito mais informativo para um leitor humano, mas não dá ao decisor capacidade de prever o
retorno relativo em 20 pregões. É o resultado esperado se essas informações já estão nos preços no dia D.
O decisor reage a isso ficando perto de 50%, o que é a resposta coerente sem vantagem.

## Piloto de insiders: pré-registro (31/10/2026, antes de qualquer retorno calculado)

**Pergunta.** Ações cujos diretores e conselheiros compraram no mercado nos últimos meses rendem mais que o
Ibovespa e que o universo líquido da B3? Nos EUA, compras de insiders são uma das evidências mais robustas
de retorno superior em 6 a 12 meses. No Brasil o dado é público, mas pouco explorado.

**Dados, sem LLM.**
- **VLMO da CVM:** formulários mensais estruturados do art. 11 da Res. 44, de dez/2018 a set/2026, de 430 a
  550 empresas por ano. Trazem cargo, compra ou venda à vista, data, quantidade, preço e volume.
- **FCA:** códigos de negociação de cada empresa.
- **Yahoo:** preços ajustados e volume.
- **Valor de cada operação:** o volume informado. Quando vier zerado, o que é comum, a quantidade vezes o
  fechamento da classe negociada (ON, PN, unit) no dia da operação.

**Regra de data.** Um formulário só conta a partir da entrega da versão que a CVM mantém. O formulário
reentregue conta da reentrega, o que é conservador.

**Universo (a cada fim de mês t).**
- Empresas com ação ou unit na B3 e um código representativo: o de maior mediana de volume financeiro nos 63
  pregões até t.
- Liquidez mínima: mediana de R$ 2 milhões por dia.
- Preço no Yahoo.

**Sinal primário.**
- Saldo, em reais, das compras menos as vendas à vista de **diretores e conselheiros de administração**
  (e vinculados), em ações e units da própria companhia.
- Formulários dos 3 meses de referência anteriores a t, entregues até t.
- Compra a empresa com saldo positivo.

**Carteira.**
- Pesos iguais entre as empresas do universo com saldo positivo. Com menos de 5 nomes, fica no universo
  inteiro naquele mês, e esse mês é marcado.
- Entrada no fechamento do pregão seguinte a t; saída no fechamento do pregão seguinte ao fim do mês
  seguinte.
- Rebalanceamento mensal, de mar/2019 a ago/2026 (90 meses).
- Custo de 0,15% por lado sobre o giro.
- Uma ação que sai da bolsa no meio do mês fica com o retorno até o último preço disponível.

**Referências.**
1. Ibovespa (`^BVSP`, índice de retorno total): o objetivo.
2. Universo líquido com pesos iguais e o mesmo custo: isola o sinal do efeito de pesos iguais.

**Métrica primária e critério para seguir.**
- Diferença mensal, líquida de custos, entre a carteira e o universo com pesos iguais, com estatística t
  sobre os meses.
- **Segue se:** t ≥ 2; diferença positiva em pelo menos 5 dos 8 anos civis (2019–2026); e retorno anual
  da carteira acima do Ibovespa no período, já líquido de custos.

**Variantes secundárias, todas reportadas.**
- (a) só controladores;
- (b) diretores, conselheiros e controladores;
- (c) só o terço com maior intensidade (saldo dividido pela mediana do volume financeiro);
- (d) janela de 6 meses;
- (e) a carteira das empresas com saldo **negativo**, que deveria render menos (teste de simetria).

**Limitações conhecidas.**
- O Yahoo não tem várias ações que saíram da bolsa. O número de empresas do VLMO sem preço é reportado.
- A CVM mantém só a última versão de cada formulário.
- Os "vinculados" (familiares, empresas ligadas) entram junto com o titular.
- São 8 anos e um único caminho histórico.

## Piloto de insiders: resultado (`insiders.py`, sem LLM, US$ 0)

Arquivos em `results/insiders/`.

**Dados.**
- 92.320 compras e vendas à vista, de 422 empresas. Em 87.282 o valor veio informado; 4.115 foram avaliadas
  pelo preço do dia; 923 ficaram sem valor.
- 350 empresas têm código na B3; dessas, 273 têm preço no Yahoo. **77 empresas ficaram de fora por falta de
  preço**: é o tamanho do viés de sobrevivência.
- O universo líquido teve em média 152 ações por mês.

| Variante | Retorno anual | Diferença contra pesos iguais | t | Anos positivos |
|---|---|---|---|---|
| **Primária: diretores e conselheiros compradores** | 5,6% | +0,11%/mês | 0,56 | 5/8 |
| (a) controladores compradores | 12,9% | +0,72%/mês | 1,98 | 5/8 |
| (b) todos os insiders | 7,4% | +0,25%/mês | 1,47 | 5/8 |
| (c) terço mais intenso | 3,4% | −0,02%/mês | −0,06 | 4/8 |
| (d) janela de 6 meses | 9,5% | +0,41%/mês | 2,34 | 5/8 |
| (e) diretores e conselheiros vendedores | 0,9% | −0,34%/mês | −1,83 | 1/8 |
| Universo líquido, pesos iguais | 4,7% | — | — | — |
| Ibovespa | 9,3% | — | — | — |

**Veredito pelo critério pré-registrado: reprovado.** A variante primária tem t = 0,56 e rende abaixo do
Ibovespa.

**Robustez das secundárias (exploratória, depois de ver os resultados).**
- **(a) Controladores:** o resultado vem de poucos nomes e de dois anos (2019 com +34% e 2024 com +31%
  contra o universo). Sem as 5 ações que mais contribuíram (AMBP3, WEGE3, EZTC3, FRAS3, PTBL3), o excesso
  cai para +0,33%/mês, com t = 0,93. Só AMBP3, em 11 meses, responde por quase um quarto do total.
- **(e) Vendedores:** sem os 5 nomes mais negativos, a diferença cai para −0,10%/mês, com t = −0,55.
- Com 6 variantes testadas, t de ~2 numa delas é o que o acaso produz com frequência.

**Achado estrutural.** De 2019 a 2026, o universo líquido com pesos iguais rendeu 4,7% ao ano, contra 9,3%
do Ibovespa. As grandes empresas ganharam da ação média. Qualquer seleção dentro do universo líquido parte de
uma desvantagem de ~4,6 pontos ao ano em relação ao índice.

Leitura: o sinal de insiders, na forma testada, não gera vantagem robusta na B3 de 2019 a 2026.

## Dividendos entre as grandes: pré-registro (31/10/2026, antes de qualquer retorno calculado)

**Pergunta.** Uma carteira das grandes pagadoras de dividendos, montada por regra, ganha do Ibovespa e do
DIVO11 (o ETF de dividendos, a alternativa pronta)? Restringir às grandes evita a desvantagem estrutural vista
no piloto de insiders: a ação média perdeu do índice.

**Dados, sem LLM.**
- **Yahoo:** preços, volume e proventos (data ex), a partir de 2008.
- **FCA da CVM:** códigos de cada empresa e setor de atividade.

**Universo (a cada rebalanceamento t).**
- As **50 empresas de maior liquidez**: mediana do volume financeiro nos 63 pregões até t, pelo código mais
  negociado de cada uma.

**Regras.**
- **Constância:** proventos com data ex em cada um dos três períodos de 12 meses antes de t.
- **Rendimento (DY24):** soma dos proventos dos últimos 24 meses dividida por 2, sobre o preço de fechamento
  em t.
- **Carteira primária:** as **10** de maior DY24 entre as constantes, com pesos iguais.
- **Rebalanceamento trimestral** (fins de mar/jun/set/dez), de mar/2012 a jun/2026. Entrada no pregão
  seguinte; pesos variam com o preço dentro do trimestre.
- **Custos e proventos:** 0,15% por lado sobre o giro; proventos reinvestidos (preço ajustado).

**Referências.**
1. Ibovespa (`^BVSP`, retorno total).
2. DIVO11, a régua real.
3. As 50 maiores com pesos iguais e o mesmo custo.

**Critério para seguir (todos):**
1. Retorno anual líquido acima do Ibovespa **e** do DIVO11 no período.
2. Diferença mensal contra o Ibovespa com t ≥ 1,65.
3. Diferença positiva em pelo menos 60% das janelas móveis de 36 meses.

Também reportados: maior queda, pior ano, diferença ano a ano, giro e DY médio da carteira.

**Variantes secundárias, todas reportadas.**
- (a) as 15 maiores;
- (b) só setores BEST: bancos e intermediação financeira, energia elétrica, saneamento, seguradoras e
  telecomunicações, incluindo as holdings de cada setor;
- (c) DY de 12 meses;
- (d) sem o filtro de constância;
- (e) rebalanceamento semestral;
- (f) universo das 100 maiores.

**Limitações conhecidas.**
- O Yahoo não tem várias pagadoras que saíram da bolsa (Cielo, EDP Brasil, AES Tietê, BR Malls, Copel PNB).
  Como algumas caíram e outras foram compradas com prêmio, o efeito tem sinal incerto.
- O Yahoo trata o JCP como bruto, o que superestima levemente o retorno das pagadoras de JCP (15% de IR
  retido).
- O IR sobre dividendos a partir de 2026 não é modelado.
- É um único caminho histórico de ~14 anos.

## Dividendos entre as grandes: resultado (`dividend_tilt.py`, sem LLM, US$ 0)

**Correção de dados depois da primeira rodada.** A primeira rodada, guardada em `results/dividends/raw/`,
deu 12,5% ao ano com t = 1,64. Dois erros de ajuste de eventos corporativos no Yahoo apareceram:
- **PCAR3:** salta +112% no dia da cisão do Assaí (01/03/2021). Ele estava na carteira em 6 variantes.
- **AZUL3:** os grupamentos de 2026 levam a referência de pesos iguais a −100%.

A regra adotada vale para todas as séries: alta diária acima de 100% ou queda além de 90% é tratada como erro
e zerada. Quedas reais como a da Americanas (−77%) ficam. Foram neutralizados 3 dias do PCAR3 e 3 da AZUL3,
entre outros. Os números abaixo são os corrigidos.

**Primária (abr/2012 a out/2026):**

| | Retorno anual | Maior queda | Diferença mensal contra a carteira | t | Janelas de 36 meses positivas | Anos positivos |
|---|---|---|---|---|---|---|
| **Carteira primária (10 maiores DY entre as 50)** | **11,2%** | −59% | — | — | — | — |
| Ibovespa | 7,5% | −47% | +0,35% | 1,41 | 83% | 10/15 |
| DIVO11 | 9,8% | −56% | +0,20% | 0,98 | 78% | 8/15 |
| 50 maiores, pesos iguais | 8,8% | −49% | +0,25% | 0,91 | 53% | 9/15 |

O DY médio da carteira foi de 9,3% e o giro trimestral (soma das mudanças de peso) de 0,56.

**Veredito pelo critério pré-registrado: reprovado.** O retorno ficou acima do Ibovespa e do DIVO11, e 83% das
janelas de 36 meses foram positivas, mas o t de 1,41 está abaixo de 1,65.

| Variante | Retorno anual | t contra o Ibovespa | Janelas de 36 meses positivas |
|---|---|---|---|
| (a) 15 maiores | 11,0% | 1,62 | 81% |
| (b) só BEST | 11,0% | 1,28 | 73% |
| (c) DY de 12 meses | 10,5% | 1,26 | 81% |
| (d) sem constância | 11,1% | 1,34 | 81% |
| (e) semestral | 13,5% | 1,74 | 86% |
| (f) universo das 100 | 14,0% | 1,68 | 82% |

As 7 versões ganham do Ibovespa e do DIVO11 em retorno anual. Duas secundárias passam de 1,65, mas sendo 2 de
6, isso não confirma nada.

**Robustez (exploratória).**
- **Sem a PETR4,** a ação cuja retirada mais pesa: +2,7 pontos ao ano sobre o Ibovespa (t = 1,09).
- **Sem 2016:** +1,4 ponto ao ano (t = 0,61). Em 2016, a carteira fez +46 pontos sobre o índice.
- **Primeira metade (2012–2019):** +3,2 pontos ao ano (t = 0,86).
- **Segunda metade (2019–2026):** +5,0 pontos ao ano (t = 1,99).

Leitura: é a primeira ideia com **sinal positivo consistente** em todas as variantes, nas duas metades e sem a
principal ação. Mas a vantagem não é estatisticamente firme, depende bastante de 2016 e cai mais nas crises.
Quase toda a vantagem sobre o Ibovespa vem de inclinar a carteira para dividendos, o que o DIVO11 já entrega.
A regra própria acrescenta ~1,4 ponto ao ano sobre o DIVO11, com t ≈ 1, e um giro que, para pessoa física com
vendas acima de R$ 20 mil por mês, gera IR não modelado aqui.

## Filtro de armadilhas de dividendo: pré-registro (31/10/2026, antes de qualquer resposta do LLM)

**Pergunta.** Um LLM que lê os demonstrativos **anonimizados** identifica, entre as maiores pagadoras, as que
vão cortar dividendos? E melhor que uma regra simples sem LLM?

**Candidatas.**
- A cada semestre (fins de junho e dezembro, de jun/2012 a jun/2026), as 15 de maior DY24 entre as 50 mais
  negociadas com proventos nos 3 anos anteriores (regras de `dividend_tilt.py`).

**O que o LLM lê.**
- Até 3 DFPs e o ITR mais recente, como entregues à CVM até a data.
- Tudo em percentual: demonstração de resultado e fluxo de caixa sobre a receita; balanço sobre o ativo total.
- Crescimento anual, payout (proventos pagos no fluxo de caixa sobre o lucro) e proventos por ação por ano,
  com o último igual a 100.
- DY de 12 e de 24 meses e o setor.
- Sem nome, ticker, CNPJ nem datas.

**O que ele responde.** A probabilidade de os proventos por ação dos próximos 12 meses ficarem ao menos 30%
abaixo dos últimos 12 meses.

**Modelo e custo.** Sonnet 5.5, esforço baixo, saída estruturada; teto de US$ 10.

**Teste de vazamento.** Em 30 pacotes sorteados, o modelo é perguntado que empresa é aquela; a taxa de acerto
é reportada.

**Regra sem LLM (a comparar).** Uma bandeira para cada um destes sinais no último ano fiscal: prejuízo; lucro
caindo mais de 30%; proventos pagos acima do lucro. A nota vai de 0 a 3.

**Desfecho.** Corte = proventos por ação com data ex nos 12 meses seguintes abaixo de 70% dos 12 meses
anteriores (Yahoo). Retorno = do pregão seguinte à data até o próximo rebalanceamento, com preços corrigidos.

**Métrica primária e critério para adotar o filtro na rotina.**
- AUC da probabilidade do LLM para prever o corte, com IC95% por bootstrap das datas.
- **Adota se:** o IC ficar acima de 0,5 **e** o AUC do LLM superar o da regra sem LLM.

**Secundárias, todas reportadas.**
- Carteira das 10 maiores DY entre as candidatas:
  - sem filtro;
  - excluindo as com probabilidade ≥ 50%;
  - excluindo as com alguma bandeira da regra.
- Diferença semestral pareada e t sobre os semestres.
- Brier contra a taxa de corte.
- Cortes evitados.

## Filtro de armadilhas de dividendo: resultado (`filter_v1`, US$ 3,12)

Foram 419 avaliações e 30 testes de identificação, sem falhas. 404 candidatas têm desfecho conhecido, e 35% delas
cortaram proventos em 30% ou mais.

**Teste de vazamento.** Em 30 pacotes anonimizados, o modelo respondeu "unknown" em 28, errou um (chamou de
Rumo uma ECOR3) e acertou um (ABEV3, com 8% de confiança). A anonimização funcionou.

**Previsão do corte (primária).**

| | AUC | IC95% (bootstrap das datas) | Brier |
|---|---|---|---|
| **LLM** | **0,693** | 0,639 a 0,742 | 0,210 (taxa base: 0,229) |
| Regra sem LLM (prejuízo, queda de lucro, payout acima de 100%) | 0,495 | 0,453 a 0,537 | — |
| LLM menos regra | — | +0,144 a +0,247 | — |

- **Critério pré-registrado: atendido.** O IC do AUC fica acima de 0,5 e acima do AUC da regra.
- **Marcação do LLM (probabilidade ≥ 50%):** 51 candidatas, das quais 67% cortaram.
- **Marcação da regra:** 109 candidatas, das quais 36% cortaram, praticamente a taxa base.
- **Checagem exploratória contra dividendo extraordinário** (proventos dos últimos 12 meses sobre a média dos 3
  anos anteriores):
  - essa regra simples atinge AUC de 0,62, e a razão DY12/DY24 de 0,64;
  - dentro de cada terço de "extraordinário", o LLM mantém AUC de 0,66 a 0,71;
  - portanto ele acrescenta informação além desse padrão.

**Carteira (secundária, 28 semestres).**

| Carteira das 10 maiores DY entre as 15 candidatas | Retorno anual | Cortes na carteira |
|---|---|---|
| Sem filtro | 13,9% | 112 |
| Excluindo marcadas pelo LLM | 11,4% | 89 |
| Excluindo marcadas pela regra | 11,1% | 95 |

- **LLM menos sem filtro:** −1,5%/semestre, t = −1,66, positivo em 9 de 28 semestres.
- **As marcadas pelo LLM renderam mais no semestre seguinte** (+10,7%) do que as não marcadas (+7,1%).

Leitura: é a primeira habilidade mensurável do LLM neste projeto. Lendo demonstrativos anonimizados, ele prevê
cortes de dividendo bem melhor que uma regra contábil e que o padrão de dividendo extraordinário. **Mas o mercado
já precifica o corte:** as ações com corte provável estão baratas e se recuperam, e excluí-las reduz o retorno.
Apesar de o critério de previsão ter sido atendido, a recomendação é **não** usar o filtro para excluir ações
de uma carteira que busca ganhar do Ibovespa. Ele serve como alerta e para quem prioriza a estabilidade da renda:
21% menos cortes, ao custo de ~2,5 pontos de retorno ao ano.

## Ações ou CDI pelo valuation: pré-registro (31/10/2026, antes de qualquer retorno calculado)

**Pergunta.** Ficar em ações só quando elas estão baratas em relação à renda fixa, e no CDI no resto do tempo,
ganha de ficar sempre em ações? O filtro de tendência falhou (4,0% contra 5,8% ao ano); este usa valuation.

**Sinal (a cada fim de mês t, só com dados conhecidos em t).**
- **Rendimento das ações:** mediana do DY24 das 50 empresas mais negociadas, com as regras de `dividend_tilt.py` e
  preços corrigidos.
- **Juro real:** Selic meta (SGS 432) em t menos o IPCA de 12 meses (SGS 13522) do último mês já divulgado. A
  divulgação é contada 12 dias após o fim do mês.
- **Spread:** rendimento das ações menos o juro real.
- **Regra:** spread acima da **mediana histórica até t** (janela crescente, com pelo menos 36 meses) → ações; senão
  → CDI.

**Carteiras.**
- **(A) Primária:** Ibovespa (`^BVSP`) ou CDI (SGS 12).
- **(B)** a carteira de dividendos semestral (`curve_e_semiannual.csv`) ou CDI.
- **Referências:** comprar e manter o Ibovespa e comprar e manter a carteira de dividendos.
- **Execução:** mudança no mês seguinte ao sinal, com 0,1% de custo a cada troca.

**Período.** Sinais de jan/2013 (os 36 meses de histórico do spread começam em 2010) a set/2026.

**Critério para seguir (A contra Ibovespa):**
1. Retorno anual maior **e** maior queda menor.
2. Diferença positiva em pelo menos 60% das janelas de 36 meses.
3. t ≥ 1,65 na diferença mensal.

(B) é reportada como secundária, contra a carteira de dividendos.

## Ações ou CDI pelo valuation: resultado (`allocation.py`, US$ 0)

Foram 166 meses (fev/2013 a out/2026), 48% deles em ações, com 7 trocas.

| | Retorno anual | Maior queda | Diferença mensal | t | Janelas de 36 meses positivas |
|---|---|---|---|---|---|
| **(A) Ibovespa ou CDI** | **6,7%** | −37% | −0,15% | −0,49 | 42% |
| Ibovespa, comprar e manter | 7,6% | −37% | — | — | — |
| (B) Dividendos ou CDI | 10,4% | −45% | −0,35% | −1,06 | 14% |
| Carteira de dividendos semestral | 13,9% | −54% | — | — | — |
| CDI | 9,9% | 0% | — | — | — |

**Veredito: reprovado.** A alternância rendeu menos que ficar sempre em ações, nas duas versões, e não reduziu a
queda máxima do Ibovespa. Ela ficou no CDI em anos fortes para a bolsa (2017, 2018, 2025) e não protegeu de forma
consistente.

Dado de contexto: de 2013 a 2026, o CDI (9,9%) rendeu mais que o Ibovespa (7,6%), e a carteira de dividendos
(13,9%) rendeu mais que os dois. Em out/2026 o sinal está no CDI: rendimento mediano de 4,6% contra juro real de
9,5%.

Leitura: tanto o filtro de tendência quanto o de valuation falham em decidir *quando* estar em ações no Brasil. A
alocação entre ações e renda fixa fica a critério do investidor, e a rotina cuida da parte em ações.

## Replay da rotina real: pré-registro (31/10/2026, antes de qualquer retorno calculado)

**Pergunta.** A revisão 3 (A2, B1) apontou que a estratégia testada não é a executada: o backtest de dividendos
rebalanceia uma carteira de valor fixo sem atrito, enquanto a rotina compra ações inteiras com aportes mensais,
vende só dentro da isenção de R$ 20 mil, adia o resto, paga IR e congela a seleção entre rebalanceamentos. A
vantagem histórica sobrevive a isso?

**O que roda (`routine_replay.py`, sem LLM).** O código de produção, mês a mês:
- **Seleção:** `selection.snapshot` e `selection.target` (as 50 mais negociadas, as 10 maiores DY24 entre as
  constantes), com a regra atual de saltos de preço (`b3_market.clean`: só saltos que se desfazem são zerados; o
  resto fica em quarentena por um ano).
- **Ordens:** `orders.plan_orders`, com isenção, adiamento, vendas pendentes retomadas, prejuízo acumulado e IR
  separado do caixa.
- **Calendário:** decisão no último pregão de cada mês, de mar/2012 a set/2026. Rebalanceamento em junho e
  dezembro; nos outros meses, a seleção congelada do último rebalanceamento e só compras. A primeira seleção é
  a de mar/2012.
- **Execução:** no fechamento do pregão seguinte. As vendas vêm primeiro; cada compra é limitada ao caixa
  disponível. Custo de 0,15% sobre o valor de cada ordem.
- **Proventos:** creditados em caixa na data ex, pela quantidade em carteira, brutos (o Yahoo não separa o JCP).
  O caixa não rende.
- **IR:** 15% sobre o ganho líquido do mês quando as vendas passam de R$ 20 mil, depois de compensar o prejuízo
  acumulado; pago do caixa no mesmo mês.
- **Dados:** os da seção "Dividendos entre as grandes": FCA 2010–2026 e preços do Yahoo até 30/10/2026, mesmo
  cache.

**Escalas (fluxos de caixa iguais para todos).**
- **A:** começa do zero, com R$ 3.000 no último pregão de cada mês.
- **B:** R$ 100.000 no início, mais R$ 3.000 por mês.

**Comparações, com os mesmos fluxos e nas mesmas datas.**
1. DIVO11, comprado a cada aporte, com frações e sem custo (preço ajustado, proventos reinvestidos).
2. BOVA11, idem.
3. CDI líquido de 15% de IR, aproximado.

**Medidas.**
- Valor final e total aportado.
- TIR anual (retorno ponderado pelo dinheiro).
- Retorno anual e maior queda da cota (retorno ponderado pelo tempo).
- IR pago, meses com venda tributável, vendas adiadas e caixa parado médio.

**Variante, reportada mas não decisiva:** rebalanceamento trimestral (mar/jun/set/dez), que foi a primária do
backtest. A semestral foi adotada depois de ver as duas (B1, risco de seleção).

**Decisão pré-registrada (escala B, semestral).**
- Se a TIR da rotina **não superar a do DIVO11 em pelo menos 1 ponto ao ano**, a documentação passa a
  recomendar o DIVO11 como veículo padrão para a parte em ações. Nesse caso a rotina fica como opção de quem
  quer a regra própria, com o aviso de que ela não mostrou vantagem depois dos atritos.
- Se superar, a rotina segue como está, com o aviso de que a vantagem não é estatisticamente firme (t de 1,41
  no backtest).

**Limitações conhecidas.** As mesmas do backtest: viés de sobrevivência do Yahoo, JCP bruto, um único caminho
histórico. Além disso, o IR sobre proventos de 2026 não é modelado.

## Replay da rotina real: resultado (`routine_replay.py`, sem LLM, US$ 0)

Foram 175 decisões mensais, de mar/2012 a set/2026, com valor final em 30/10/2026. O valor final está antes do
IR sobre ganhos não realizados.

| Escala B (R$ 100 mil + R$ 3 mil/mês) | TIR | Retorno anual da cota | Maior queda da cota | Valor final | IR pago |
|---|---|---|---|---|---|
| **Rotina, semestral (a adotada)** | **12,0%** | 10,2% | −59% | R$ 1,87 mi | R$ 6 mil |
| Rotina, trimestral | 12,1% | 10,4% | −59% | R$ 1,88 mi | R$ 2 mil |
| DIVO11, mesmos aportes | 11,1% | 9,9% | −52% | R$ 1,72 mi | — |
| BOVA11, mesmos aportes | 8,9% | 7,6% | −39% | R$ 1,39 mi | — |
| CDI líquido | 8,4% | 8,3% | 0% | R$ 1,33 mi | — |

Total aportado: R$ 625 mil.

| Escala A (do zero, R$ 3 mil/mês) | TIR | Retorno anual da cota | Maior queda da cota | Valor final |
|---|---|---|---|---|
| Rotina, semestral | 12,4% | 9,9% | −59% | R$ 1,40 mi |
| Rotina, trimestral | 13,5% | 10,9% | −54% | R$ 1,53 mi |
| DIVO11 | 12,2% | 9,9% | −52% | R$ 1,37 mi |
| BOVA11 | 9,9% | 7,6% | −39% | R$ 1,13 mi |
| CDI líquido | 8,5% | 8,3% | 0% | R$ 1,02 mi |

Total aportado: R$ 525 mil.

**Veredito pelo critério pré-registrado: a rotina não passa.** Na escala B semestral, a TIR da rotina superou a do
DIVO11 em **0,88 ponto ao ano**, abaixo do mínimo de 1 ponto. Pela regra fixada antes, a documentação passa a
recomendar o **DIVO11 como veículo padrão** para a parte em ações. A rotina fica como opção de quem quer a regra
própria, com o aviso de que não mostrou vantagem firme depois dos atritos.

**O que o replay mostra:**
- **Os atritos comem a maior parte da vantagem do backtest.** A cota da rotina semestral rendeu 10,2% ao ano,
  contra 13,5% no backtest sem atrito. A trimestral rendeu 10,4%, contra 11,2%. Pesam:
  - a seleção congelada entre rebalanceamentos, com o dinheiro novo entrando só nas ações da seleção;
  - as vendas presas à isenção: houve vendas adiadas em 127 dos 175 meses, porque com a carteira grande R$ 20 mil
    por mês não dão conta da troca;
  - a compra em ações inteiras.
- **B1 perde importância.** Na rotina real, semestral e trimestral ficam praticamente iguais na escala B (12,0%
  e 12,1%). A diferença de 13,5% contra 11,2% do backtest não sobrevive.
- **A queda é maior que a do DIVO11** (−59% contra −52%), com o mesmo retorno da cota.
- **IR baixo.** Em 71 meses as vendas executadas passaram um pouco de R$ 20 mil, porque o preço de execução difere
  do de planejamento. Isso custou R$ 6 mil em 14 anos; um investidor real ajustaria a quantidade.

**Fora do critério, mas relevante para a escolha:**
- **Vendas de ETF não têm a isenção de R$ 20 mil.** Quem um dia vender o DIVO11 paga 15% sobre todo o ganho. Na
  escala B, isso dá cerca de R$ 160 mil se tudo for vendido de uma vez. Ações podem ser vendidas aos poucos
  dentro da isenção.
- **Proventos:** o DIVO11 reinveste internamente e cobra taxa de administração, já embutida no preço. As ações
  pagam em conta: a rotina reinveste, mas um investidor pode querer usar a renda.
- **Mesmas limitações do backtest:** viés de sobrevivência e JCP bruto. O viés de sobrevivência tem sinal
  incerto (revisão 4). O JCP bruto favorece a rotina, ainda mais desde 1º/1/2026, quando o IR retido no JCP
  passou de 15% para 17,5% (LC 224/2025). O Yahoo não separa JCP de dividendo, então o efeito não foi calculado.
- **Outros atritos não modelados:**
  - os proventos entram na data ex, e não na de pagamento;
  - o caixa parado não rende.

  Dividendos acima de R$ 50 mil por mês da mesma empresa pagam 10% desde 2026 (Lei 15.270/2025), o que não
  alcança carteiras deste tamanho.

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
